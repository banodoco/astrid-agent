"""Add async Comfy poll-sleep counts to the unchanged LS-2 observer.

Only instrumented LS-5 runs install this wrapper, after the original admission
wrapper just as LS-2 does. It returns the original coroutine without replacing
its wait, clock, result or exception. All existing LS-2 observations remain.
"""


def with_async_sleeps(base):
    import asyncio
    import sys

    class AsyncSleepObserver(base):
        def start(self):
            super().start()
            original = asyncio.sleep

            def observed_sleep(delay, result=None):
                self.event("ls5_async_sleep_attempt")
                caller = sys._getframe(1).f_code
                if (caller.co_name == "_wait_for_server_history"
                        and caller.co_filename.replace("\\", "/").endswith("/vibecomfy/runtime/session.py")):
                    self.event("ls5_comfy_poll_sleep_attempt")
                return original(delay, result)

            self.patches.setattr(asyncio, "sleep", observed_sleep)

        def snapshot(self, *args, **kwargs):
            report = super().snapshot(*args, **kwargs)
            for name in ("ls5_async_sleep_attempt", "ls5_comfy_poll_sleep_attempt"):
                report["counts"].setdefault(name, 0)
            report["ls5_async_sleep_observed"] = True
            return report

    return AsyncSleepObserver
