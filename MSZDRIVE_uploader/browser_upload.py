from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any


class MszBrowserUploader:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        storage_state_path: Path,
        headless: bool = True,
        executable_path: str = "",
        folder_url: str = "",
        timeout_ms: int = 120_000,
        verbose: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email.strip()
        self.password = password
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.executable_path = executable_path.strip()
        self.folder_url = folder_url.strip()
        self.timeout_ms = timeout_ms
        self.verbose = verbose
        if not self.email or not self.password:
            raise ValueError("MSZ_EMAIL and MSZ_PASSWORD are required for browser uploads.")

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[browser] {message}", flush=True)

    async def upload(self, file_path: Path, target_folder: str) -> dict[str, Any]:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for browser uploads. Install requirements first.") from exc

        upload_requests: list[str] = []
        upload_responses: list[dict[str, Any]] = []

        async with async_playwright() as playwright:
            self._log(f"launching Chromium for {file_path.name}")
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            executable = self.executable_path or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
            if executable and Path(executable).exists():
                launch_kwargs["executable_path"] = executable
            elif executable:
                self._log(f"Chromium executable not found at {executable}; using Playwright bundled Chromium")
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {}
            if self.storage_state_path.exists():
                self._log(f"loading cached browser session: {self.storage_state_path}")
                context_kwargs["storage_state"] = str(self.storage_state_path)
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)

            def _track_request(request):
                if "upload" in request.url.lower():
                    upload_requests.append(request.url)

            async def _track_response(response):
                if "upload" not in response.url.lower():
                    return
                record: dict[str, Any] = {"url": response.url, "status": response.status}
                try:
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "json" in content_type:
                        record["json"] = await response.json()
                except Exception:
                    pass
                upload_responses.append(record)

            page.on("request", _track_request)
            page.on("response", _track_response)

            try:
                await self._ensure_logged_in(page, context)
                await self._open_target_location(page, target_folder)
                await self._choose_upload_file(page, file_path, PlaywrightTimeoutError)
                await self._wait_for_upload_completion(page, file_path.name, file_path.stat().st_size, PlaywrightTimeoutError)
                self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(self.storage_state_path))
                self._log(f"browser upload completed: {file_path.name}")
            except Exception:
                await self._dump_debug_artifacts(page, file_path)
                raise
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

        return {"requests": upload_requests, "responses": upload_responses}

    async def _ensure_logged_in(self, page, context) -> None:
        self._log(f"opening {self.base_url}/drive")
        await page.goto(self.base_url + "/drive", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        if await self._looks_logged_in(page):
            self._log("cached session appears logged in")
            return

        self._log("opening login page")
        await page.goto(self.base_url + "/login", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        if await self._looks_logged_in(page):
            self._log("login page redirected to drive; session is active")
            return

        email_input = page.locator('input[name="email"], input[type="email"], input[name*="email" i]').first
        password_input = page.locator('input[name="password"], input[type="password"]').first
        if await email_input.count() == 0 or await password_input.count() == 0:
            visible = await self._visible_text_sample(page)
            raise RuntimeError(
                f"MSZ login form was not found. Current URL: {page.url}. Visible text sample: {visible}"
            )
        await email_input.fill(self.email)
        await password_input.fill(self.password)

        self._log("submitting login form")
        button = page.get_by_role("button", name="Continue").or_(
            page.get_by_role("button", name="Sign in")
        ).or_(page.get_by_role("button", name="Login"))
        await button.first.click()
        await page.wait_for_load_state("networkidle")
        if not await self._looks_logged_in(page):
            await page.goto(self.base_url, wait_until="networkidle")
        if not await self._looks_logged_in(page):
            raise RuntimeError("MSZ browser login did not reach the drive UI.")
        self._log("login succeeded")
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(self.storage_state_path))

    async def _looks_logged_in(self, page) -> bool:
        try:
            password_count = await page.locator('input[type="password"]').count()
            file_input_count = await page.locator('input[type="file"]').count()
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2_000)
            except Exception:
                body_text = ""
            drive_markers = (
                "Upload" in body_text
                or "All files" in body_text
                or "Shared with me" in body_text
                or "Recent" in body_text
            )
            return password_count == 0 and (file_input_count > 0 or drive_markers)
        except Exception:
            return False

    async def _open_target_location(self, page, target_folder: str) -> None:
        if self.folder_url:
            self._log(f"opening browser folder URL: {self.folder_url}")
            await page.goto(self.folder_url, wait_until="networkidle")
            return

        self._log(f"opening drive root; target folder hint: {target_folder}")
        await page.goto(self.base_url + "/drive", wait_until="networkidle")
        for segment in [part for part in target_folder.split("/") if part.strip()]:
            folder_name = segment.strip()
            if not await self._open_folder_if_visible(page, folder_name):
                await self._create_folder(page, folder_name)
                if not await self._open_folder_if_visible(page, folder_name):
                    raise RuntimeError(f"Created or searched for folder, but could not open it: {folder_name}")

    async def _open_folder_if_visible(self, page, folder_name: str) -> bool:
        candidates = [
            page.locator('[role="row"]').filter(has_text=folder_name).first,
            page.get_by_text(folder_name, exact=True).first,
        ]
        for locator in candidates:
            try:
                if await locator.count() == 0:
                    continue
                self._log(f"opening folder: {folder_name}")
                await locator.dblclick(timeout=5_000)
                await page.wait_for_load_state("networkidle")
                return True
            except Exception:
                continue
        self._log(f"folder not visible yet: {folder_name}")
        return False

    async def _open_upload_menu(self, page) -> None:
        self._log("opening Upload menu")
        upload_button = page.get_by_role("button", name="Upload").or_(
            page.get_by_text("Upload", exact=True)
        ).first
        await upload_button.click(timeout=15_000)

    async def _create_folder(self, page, folder_name: str) -> None:
        self._log(f"creating folder: {folder_name}")
        await self._open_upload_menu(page)
        create_item = page.get_by_role("menuitem", name="Create folder").or_(
            page.get_by_text("Create folder", exact=True)
        ).first
        await create_item.click(timeout=15_000)

        input_box = page.locator('input[type="text"], input:not([type]), textarea').last
        await input_box.fill(folder_name, timeout=15_000)

        submit = page.get_by_role("button", name="Create").or_(
            page.get_by_role("button", name="Create folder")
        ).or_(page.get_by_role("button", name="Save")).first
        await submit.click(timeout=15_000)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1_000)

    async def _choose_upload_file(self, page, file_path: Path, timeout_error) -> None:
        self._log("looking for upload control")
        input_locator = page.locator('input[type="file"]').first
        if await input_locator.count() > 0:
            self._log("using existing file input")
            await input_locator.set_input_files(str(file_path))
            return

        last_error = None
        try:
            await self._open_upload_menu(page)
            upload_files_item = page.get_by_role("menuitem", name="Upload files").or_(
                page.get_by_text("Upload files", exact=True)
            ).first
            async with page.expect_file_chooser(timeout=15_000) as chooser_info:
                self._log("clicking Upload files")
                await upload_files_item.click(timeout=10_000)
            chooser = await chooser_info.value
            await chooser.set_files(str(file_path))
            self._log(f"selected file: {file_path.name}")
            return
        except Exception as exc:
            last_error = exc

        # Fallback for builds that expose a hidden file input after the menu item
        # click but do not emit Playwright's file chooser event.
        try:
            input_locator = page.locator('input[type="file"]').first
            if await input_locator.count() > 0:
                await input_locator.set_input_files(str(file_path))
                self._log(f"selected file through fallback input: {file_path.name}")
                return
        except Exception as exc:
            last_error = exc

        visible = await self._visible_text_sample(page)
        raise RuntimeError(
            "Could not find an MSZ upload file picker. "
            f"Current URL: {page.url}. Last error: {last_error}. Visible text sample: {visible}"
        )

    async def _wait_for_upload_completion(self, page, file_name: str, size: int, timeout_error) -> None:
        deadline = asyncio.get_running_loop().time() + max(self.timeout_ms / 1000, 120)
        last_error = None
        last_report = 0.0
        saw_upload_toast = False
        completed_at = 0.0
        self._log(f"waiting for upload progress: {file_name}")
        while asyncio.get_running_loop().time() < deadline:
            try:
                failed = page.get_by_text("failed", exact=False)
                if await failed.count() > 0:
                    raise RuntimeError("MSZ web UI reported an upload failure.")

                progress_text = await self._upload_progress_text(page, file_name)
                now = time.monotonic()
                if progress_text and now - last_report >= 2.0:
                    last_report = now
                    self._log(f"upload progress: {progress_text}")

                if progress_text:
                    saw_upload_toast = True
                    if self._progress_looks_done(progress_text, size):
                        if completed_at == 0.0:
                            completed_at = now
                        if now - completed_at >= 2.0:
                            return
                elif saw_upload_toast:
                    # Some builds remove the upload toast immediately after completion.
                    if completed_at == 0.0:
                        completed_at = now
                    if now - completed_at >= 5.0:
                        return
            except timeout_error:
                pass
            except Exception as exc:
                last_error = str(exc)
                if "upload failure" in last_error.lower():
                    raise
            await asyncio.sleep(2)
        raise RuntimeError(last_error or f"Timed out waiting for browser upload to finish: {file_name}")

    async def _upload_progress_text(self, page, file_name: str) -> str:
        selectors = [
            ".fixed.bottom-16.right-16",
            "[class*='bottom-16'][class*='right-16']",
            "text=Uploading",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    text = " ".join((await locator.inner_text(timeout=1_000)).split())
                    if file_name in text or "Uploading" in text:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _progress_looks_done(progress_text: str, size: int) -> bool:
        lower = progress_text.lower()
        if "complete" in lower or "uploaded" in lower:
            return True
        # The toast often says "63 MB of 63 MB"; use the filename presence plus
        # disappearance as the main completion signal, so avoid over-parsing units.
        return False

    async def _visible_text_sample(self, page) -> str:
        try:
            text = await page.locator("body").inner_text(timeout=2_000)
        except Exception:
            return ""
        return " ".join(text.split())[:1000]

    async def _dump_debug_artifacts(self, page, file_path: Path) -> None:
        debug_dir = self.storage_state_path.parent / "browser_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stem = file_path.stem[:80].replace("/", "_")
        try:
            await page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True)
            self._log(f"saved screenshot: {debug_dir / f'{stem}.png'}")
        except Exception:
            pass
        try:
            (debug_dir / f"{stem}.html").write_text(await page.content(), encoding="utf-8")
            self._log(f"saved html: {debug_dir / f'{stem}.html'}")
        except Exception:
            pass
