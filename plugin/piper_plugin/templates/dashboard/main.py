#!/usr/bin/env python3
"""__NAME__ — 상태를 올리기만 하는 뼈대. 화면은 `ui/index.html` 이 `/api/plugins/__NAME__` 을 읽어 그린다."""
from piper_plugin import Plugin


class Main(Plugin):
    def setup(self):
        self.ticks = 0

    def loop(self):
        self.ticks += 1
        act = self.robot.activity() or {}
        self.state(ticks=self.ticks, activities=act.get("running", []))
        self.sleep(float(self.config["interval_s"]))


if __name__ == "__main__":
    raise SystemExit(Main.main())
