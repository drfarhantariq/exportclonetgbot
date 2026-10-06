"""Single-worker FIFO transfer queue with durable snapshots."""
from __future__ import annotations

import asyncio
import copy


class TransferQueue:
    def __init__(self):
        self.pending = []
        self.active = None
        self.condition = asyncio.Condition()

    def snapshot(self):
        return copy.deepcopy({"pending": self.pending, "active": self.active})

    def hydrate(self, snapshot, last_state=None):
        self.pending = [dict(job) for job in (snapshot or {}).get("pending", [])
                        if isinstance(job, dict) and job.get("job_id") and isinstance(job.get("argv"), list)]
        active = (snapshot or {}).get("active")
        if isinstance(active, dict) and active.get("job_id") and isinstance(active.get("argv"), list):
            completed = last_state and last_state.get("job_id") == active["job_id"] and last_state.get("phase") in {"completed", "failed", "cancelled"}
            if not completed:
                active = dict(active)
                active["argv"] = [arg for arg in active["argv"] if arg not in {"--resume", "--no-resume"}] + ["--resume"]
                active["phase"] = "queued"
                active["recovered"] = True
                self.pending = [active] + [job for job in self.pending if job["job_id"] != active["job_id"]]
        self.active = None

    async def enqueue(self, job, save):
        async with self.condition:
            if len(self.pending) >= 50:
                raise ValueError("Transfer queue is full (50 waiting jobs).")
            self.pending.append(job)
            try:
                await save(self.snapshot())
            except Exception:
                self.pending.remove(job)
                raise
            self.condition.notify_all()
            return len(self.pending)

    async def take(self, save):
        async with self.condition:
            while not self.pending:
                await self.condition.wait()
            self.active = self.pending.pop(0)
            await save(self.snapshot())
            return self.active

    async def finish(self, save):
        async with self.condition:
            self.active = None
            await save(self.snapshot())

    async def remove(self, job_id, save):
        async with self.condition:
            for index, job in enumerate(self.pending):
                if job["job_id"] == job_id:
                    removed = self.pending.pop(index)
                    removed["phase"] = "cancelled"
                    await save(self.snapshot())
                    return removed
        return None

    async def clear(self, save):
        async with self.condition:
            removed, self.pending = self.pending, []
            for job in removed:
                job["phase"] = "cancelled"
            await save(self.snapshot())
            return removed

    def find(self, job_id):
        if self.active and self.active["job_id"] == job_id:
            return self.active
        return next((job for job in self.pending if job["job_id"] == job_id), None)

    def position(self, job_id):
        return next((i + 1 for i, job in enumerate(self.pending) if job["job_id"] == job_id), None)
