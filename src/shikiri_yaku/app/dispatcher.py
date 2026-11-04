"""The Dispatcher: hands every new signal to the Router."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shikiri_yaku.app.router import Router
    from shikiri_yaku.ports import Clock, DispatchLog


class Dispatcher:
    """Polls the journal for signals in state `new`, oldest first, and submits each to the Router.

    At start it puts the signals a crash left in `handling` back to `new`, so none is lost; a
    handler may then run twice for one signal. A signal is submitted once while its
    task runs, because classifying can take longer than one interval.
    """

    def __init__(
        self, log: DispatchLog, router: Router, clock: Clock, *, interval_s: float
    ) -> None:
        self._log = log
        self._router = router
        self._clock = clock
        self._interval_s = interval_s
        self._running: set[int] = set()

    async def run(self) -> None:
        self._log.recover(self._clock.now())
        while True:
            self.tick()
            await self._clock.sleep(self._interval_s)

    def tick(self) -> None:
        for number, signal in self._log.new_signals():
            if number in self._running:
                continue
            self._running.add(number)
            self._router.submit(number, signal).add_done_callback(
                lambda _task, number=number: self._running.discard(number)
            )
