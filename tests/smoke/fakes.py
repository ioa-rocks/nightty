# SPDX-License-Identifier: MPL-2.0
"""Stand-ins for the agent backend so the smoke scenario never performs inference."""


class FakeAgents:
    calls = []
    interrupt_next = True

    def snapshot(self):
        return {"profiles": [{"id": "fixture", "name": "Research helper", "session_mode": "continuous", "execution": "Fixture", "collection": "public-fixture"}], "sessions": []}

    def create(self, profile):
        self.calls.append(("create", profile))
        return {"id": "fixture-thread"}

    def send(self, profile, session, key, text):
        self.calls.append(("send", profile, session, key, text))
        if self.interrupt_next:
            self.interrupt_next = False
            raise RuntimeError("Fixture interrupted delivery")
        return {"id": session, "status": "completed"}

    def detail(self, profile, session):
        return {"id": session, "status": "completed", "messages": [
            {"role": "assistant", "content": "Fixture response <b>kept as text</b>"}]}
