# SPDX-License-Identifier: MPL-2.0
"""Project-tab panel for the local agent service: profiles, sessions and an explicit Send."""

import uuid

from gi.repository import GLib, Gtk

from .widgets import Box, button, label, scrolled, textview


class AgentPanel:
    """Nothing here starts inference except ``send``; drafts are kept per profile."""

    def __init__(self, window, client):
        self.window, self.client = window, client
        self.snapshot, self.session = {}, None
        self.loading, self.sending, self.request_key = False, False, None
        self.drafts = {}
        self.pending_sends = {}
        self.widget = self.build()

    def build(self):
        box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=15)
        controls = Box(spacing=8)
        self.profile_picker = Gtk.ComboBoxText()
        self.session_picker = Gtk.ComboBoxText()
        controls.pack_start(self.profile_picker, False, False, 0)
        controls.pack_start(self.session_picker, True, True, 0)
        controls.pack_start(button("Refresh", self.refresh), False, False, 0)
        controls.pack_start(button("New", self.new_session), False, False, 0)
        self.profile_picker.connect("changed", lambda *_: self.profile_changed())
        self.session_picker.connect("changed", lambda *_: self.load_session())
        box.pack_start(controls, False, False, 0)
        self.info = label("Connect to the existing local agent service. Folder contents are shared only when you send them.", "muted")
        box.pack_start(self.info, False, False, 0)
        self.chat = textview()
        box.pack_start(scrolled(self.chat), True, True, 0)
        self.compose = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.compose.set_left_margin(12)
        self.compose.set_top_margin(10)
        composer = scrolled(self.compose)
        composer.set_size_request(-1, 100)
        box.pack_start(composer, False, False, 0)
        self.compose.get_buffer().connect("changed", self.draft_changed)
        self.send_button = button("Send to selected agent", self.send)
        self.send_button.set_sensitive(False)
        box.pack_start(self.send_button, False, False, 0)
        return box

    def draft_changed(self, *_):
        profile = self.profile_picker.get_active_id()
        if profile:
            buffer = self.compose.get_buffer()
            self.drafts[profile] = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            self.request_key = None

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.info.set_text("Connecting…")

        def ready(snapshot):
            self.loading = False
            selected = self.profile_picker.get_active_id() or (self.window.context or {}).get("agent")
            self.snapshot = snapshot
            self.profile_picker.remove_all()
            for profile in snapshot.get("profiles", []):
                self.profile_picker.append(profile["id"], profile["name"])
            if selected:
                self.profile_picker.set_active_id(selected)
            if self.profile_picker.get_active() < 0:
                self.profile_picker.set_active(0)
            self.profile_changed()

        def failed(exc):
            self.loading = False
            self.info.set_text("Agent service unavailable. Files and terminals remain available.")
            self.send_button.set_sensitive(False)
        self.window.async_call(self.client.snapshot, ready, failed)

    def profile_changed(self):
        identifier = self.profile_picker.get_active_id()
        if not identifier:
            return
        self.session = None
        self.session_picker.remove_all()
        for session in self.snapshot.get("sessions", []):
            if session["profile"] == identifier:
                self.session_picker.append(session["id"], session["title"])
        self.session_picker.set_active(0)
        profile = next(p for p in self.snapshot["profiles"] if p["id"] == identifier)
        self.info.set_text(f"{profile['name']} · {profile.get('session_mode', '')} · {profile.get('execution', '')} · " + (profile.get("collection") or "No Library access"))
        draft = self.drafts.get(identifier, "")
        self.compose.get_buffer().set_text(draft)
        self.send_button.set_sensitive(not self.sending)

    def new_session(self):
        identifier = self.profile_picker.get_active_id()
        if not identifier or self.sending:
            return
        self.session = None
        self.pending_sends.pop(identifier, None)
        self.session_picker.set_active(-1)
        self.chat.get_buffer().set_text("A new conversation starts when you send your first message.")

    def load_session(self):
        profile, session = self.profile_picker.get_active_id(), self.session_picker.get_active_id()
        self.session = session
        if not profile or not session:
            self.chat.get_buffer().set_text("Choose a conversation, or send a message to start one.")
            return

        def ready(result):
            if profile != self.profile_picker.get_active_id() or session != self.session:
                return
            transcript = "\n\n".join(m["role"].upper() + "\n" + m["content"] for m in result.get("messages", []))
            self.chat.get_buffer().set_text(transcript or "No messages yet.")
            if result.get("status") == "running":
                GLib.timeout_add(2000, lambda: (self.load_session(), False)[1] if session == self.session else False)
        self.window.async_call(lambda: self.client.detail(profile, session), ready)

    def send(self):
        profile = self.profile_picker.get_active_id()
        buffer = self.compose.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not profile or not text or self.sending:
            return
        mode = next(p.get("session_mode") for p in self.snapshot["profiles"] if p["id"] == profile)
        session = None if mode == "one_off" else self.session
        pending = self.pending_sends.get(profile)
        if not pending or pending["text"] != text:
            pending = {"profile": profile, "key": uuid.uuid4().hex, "text": text, "session": session}
            self.pending_sends[profile] = pending
        request_key = pending["key"]
        self.sending = True
        self.send_button.set_sensitive(False)
        self.profile_picker.set_sensitive(False)
        self.session_picker.set_sensitive(False)

        def submit():
            if pending["session"] is None:
                pending["session"] = self.client.create(profile)["id"]
            target = pending["session"]
            return self.client.send(profile, target, request_key, text)

        def ready(result):
            self.sending = False
            self.session = result["id"]
            current = self.compose.get_buffer()
            current_text = current.get_text(current.get_start_iter(), current.get_end_iter(), False).strip()
            if current_text == text:
                current.set_text("")
            self.request_key = None
            self.pending_sends.pop(profile, None)
            self.send_button.set_sensitive(True)
            self.profile_picker.set_sensitive(True)
            self.session_picker.set_sensitive(True)
            self.load_session_direct(profile, result["id"])

        def failed(exc):
            self.sending = False
            self.send_button.set_sensitive(True)
            self.profile_picker.set_sensitive(True)
            self.session_picker.set_sensitive(True)
            self.info.set_text(str(exc) + ". Your draft is retained.")
        self.window.async_call(submit, ready, failed)

    def load_session_direct(self, profile, session):
        if profile != self.profile_picker.get_active_id():
            return
        self.session_picker.append(session, "Current conversation")
        self.session_picker.set_active_id(session)
