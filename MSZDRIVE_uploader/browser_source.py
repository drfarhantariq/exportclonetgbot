from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator


@dataclass(frozen=True)
class BrowserMszItem:
    path: Path
    rel_path: str
    size: int | None = None
    cleanup: bool = True


class MszBrowserSource:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        password: str,
        storage_state_path: Path,
        headless: bool = True,
        executable_path: str = "",
        timeout_ms: int = 120_000,
        verbose: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email.strip()
        self.password = password
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.executable_path = executable_path.strip()
        self.timeout_ms = timeout_ms
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[browser-source] {message}", flush=True)

    async def iter_folder(self, folder_url: str, staging_dir: Path) -> AsyncIterator[BrowserMszItem]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for browser MSZ source fallback.") from exc

        staging_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            launch_kwargs = {
                "headless": self.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            executable = self.executable_path or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
            if executable and Path(executable).exists():
                launch_kwargs["executable_path"] = executable
            elif executable:
                self._log(f"Chromium executable not found at {executable}; using Playwright bundled Chromium")
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs = {"accept_downloads": True}
            if self.storage_state_path.exists():
                context_kwargs["storage_state"] = str(self.storage_state_path)
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                await self._ensure_logged_in(page, context)
                async for item in self._walk_folder(page, folder_url, staging_dir, ""):
                    yield item
                self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(self.storage_state_path))
            finally:
                await context.close()
                await browser.close()

    async def resolve_folder_title(self, folder_url: str) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for browser MSZ source fallback.") from exc

        async with async_playwright() as playwright:
            launch_kwargs = {
                "headless": self.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            executable = self.executable_path or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
            if executable and Path(executable).exists():
                launch_kwargs["executable_path"] = executable
            elif executable:
                self._log(f"Chromium executable not found at {executable}; using Playwright bundled Chromium")
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs = {}
            if self.storage_state_path.exists():
                context_kwargs["storage_state"] = str(self.storage_state_path)
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                await self._ensure_logged_in(page, context)
                self._log(f"Resolving MSZ folder title from browser: {folder_url}")
                await page.goto(folder_url, wait_until="networkidle")
                await page.wait_for_timeout(1500)
                title = await self._folder_title_from_page(page)
                self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(self.storage_state_path))
                return title
            finally:
                await context.close()
                await browser.close()

    async def _ensure_logged_in(self, page, context) -> None:
        await page.goto(self.base_url + "/drive", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        if await self._looks_logged_in(page):
            return
        await page.goto(self.base_url + "/login", wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        if await self._looks_logged_in(page):
            return
        email_input = page.locator('input[name="email"], input[type="email"], input[name*="email" i]').first
        password_input = page.locator('input[name="password"], input[type="password"]').first
        await email_input.fill(self.email)
        await password_input.fill(self.password)
        await self._click_login_submit(page)
        await page.wait_for_load_state("networkidle")
        if not await self._looks_logged_in(page):
            await page.goto(self.base_url + "/drive", wait_until="networkidle")
        if not await self._looks_logged_in(page):
            raise RuntimeError("MSZ browser login did not reach drive UI.")
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(self.storage_state_path))

    async def _click_login_submit(self, page) -> None:
        candidates = [
            page.locator('form button[type="submit"]').first,
            page.locator('button[type="submit"]').first,
            page.get_by_role("button", name="Continue", exact=True).first,
            page.get_by_role("button", name="Sign in", exact=True).first,
            page.get_by_role("button", name="Login", exact=True).first,
        ]
        last_error = None
        for button in candidates:
            try:
                if await button.count() == 0:
                    continue
                await button.click(timeout=15_000)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not click MSZ login submit button. Last error: {last_error}")

    async def _looks_logged_in(self, page) -> bool:
        try:
            password_count = await page.locator('input[type="password"]').count()
            body = await page.locator("body").inner_text(timeout=2_000)
            return password_count == 0 and any(marker in body for marker in ("Upload", "All files", "Shared with me"))
        except Exception:
            return False

    async def _walk_folder(
        self,
        page,
        folder_url: str,
        staging_dir: Path,
        rel_prefix: str,
    ) -> AsyncIterator[BrowserMszItem]:
        self._log(f"Opening MSZ folder URL: {folder_url}")
        await page.goto(folder_url, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        entries = await self._visible_entries(page)
        self._log(f"Visible entries: {len(entries)} in {folder_url}")
        for entry in entries:
            name = entry["name"]
            rel_path = f"{rel_prefix}/{name}".strip("/")
            if entry["type"] == "folder":
                async for child in self._walk_folder(page, entry["url"], staging_dir, rel_path):
                    yield child
                await page.goto(folder_url, wait_until="networkidle")
                continue
            local_path = staging_dir / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if local_path.exists() and local_path.stat().st_size > 0:
                self._log(f"Reusing browser download: {local_path}")
                yield BrowserMszItem(path=local_path, rel_path=rel_path, size=local_path.stat().st_size)
                continue
            self._log(f"Downloading via browser: {rel_path}")
            await self._download_entry(page, name, local_path)
            yield BrowserMszItem(path=local_path, rel_path=rel_path, size=local_path.stat().st_size)

    async def _visible_entries(self, page) -> list[dict[str, str]]:
        entries = await page.locator(".grid-item").evaluate_all(
            """
            els => els.map(el => {
              const text = (el.innerText || '').trim();
              const isFolder = !!el.querySelector('.folder-file-color');
              const anchor = el.querySelector('a[href]');
              return {name: text, type: isFolder ? 'folder' : 'file', url: anchor ? anchor.href : ''};
            }).filter(x => x.name)
            """
        )
        # Grid entries often do not contain anchors; URLs are resolved by opening
        # folders through the UI only when anchors are available.
        return [entry for entry in entries if isinstance(entry.get("name"), str)]

    async def _download_entry(self, page, name: str, local_path: Path) -> None:
        item = page.locator(".grid-item").filter(has_text=name).first
        await item.click(button="right")
        download_text = page.get_by_role("menuitem", name="Download").or_(
            page.get_by_text("Download", exact=True)
        ).first
        async with page.expect_download(timeout=self.timeout_ms) as download_info:
            await download_text.click()
        download = await download_info.value
        await download.save_as(str(local_path))

    async def _folder_title_from_page(self, page) -> str:
        breadcrumb_title = await page.evaluate(
            """
            () => {
              const stopWords = new Set([
                'Upload', 'All Files', 'Shared with me', 'Recent', 'Starred',
                'Trash', 'Name', 'Owner', 'Last modified', 'Date modified',
                'Type', 'People', 'Modified', 'Source'
              ]);
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const scored = [];
              for (const el of document.querySelectorAll('a,button,[role="button"],[aria-current],h1,h2')) {
                const text = clean(el.innerText || el.textContent || '');
                if (!text || text.length > 160 || stopWords.has(text)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                let score = 0;
                if (rect.top < 140) score += 6;
                if (rect.left > 120 && rect.left < window.innerWidth - 40) score += 3;
                if ((el.getAttribute('href') || '').includes('/drive/folders/')) score += 4;
                if ((el.getAttribute('aria-current') || '').toLowerCase() === 'page') score += 8;
                if ((el.closest('nav') || el.closest('[aria-label*="breadcrumb" i]'))) score += 6;
                scored.push({text, score, top: rect.top, left: rect.left});
              }
              scored.sort((a, b) => b.score - a.score || b.left - a.left || a.top - b.top);
              return scored.length ? scored[0].text : '';
            }
            """
        )
        if isinstance(breadcrumb_title, str) and breadcrumb_title.strip():
            return breadcrumb_title.strip()

        body = await page.locator("body").inner_text(timeout=5_000)
        lines = [" ".join(line.split()) for line in body.splitlines()]
        lines = [line for line in lines if line]
        stop = {
            "Upload",
            "Shared with me",
            "Recent",
            "Starred",
            "Trash",
            "Name",
            "Owner",
            "Last modified",
            "Date modified",
            "Type",
            "People",
            "Modified",
            "Source",
        }
        best: list[str] = []
        for index, line in enumerate(lines):
            if line != "All Files":
                continue
            crumbs: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate in stop or candidate.lower().startswith("last "):
                    break
                if candidate not in crumbs and len(candidate) <= 160:
                    crumbs.append(candidate)
            if len(crumbs) > len(best):
                best = crumbs
        return best[-1] if best else ""
