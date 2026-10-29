# SPDX-License-Identifier: MPL-2.0
"""Small GTK 4 helpers: packing-order box, event controllers and dialog builders."""

import argparse

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, Gtk, Pango, Vte


class Box(Gtk.Box):
    """Keep Nightty's start/end packing order while using GTK 4 boxes."""

    def __init__(self, **kwargs):
        margin = kwargs.pop("margin", 0)
        super().__init__(**kwargs)
        if margin:
            for edge in ("start", "end", "top", "bottom"):
                getattr(self, f"set_margin_{edge}")(margin)
        self._start_tail = None

    def _layout_child(self, child, expand, padding):
        if self.get_orientation() == Gtk.Orientation.HORIZONTAL:
            child.set_hexpand(expand)
            child.set_margin_start(padding)
            child.set_margin_end(padding)
        else:
            child.set_vexpand(expand)
            child.set_margin_top(padding)
            child.set_margin_bottom(padding)

    def pack_start(self, child, expand=False, _fill=False, padding=0):
        self._layout_child(child, expand, padding)
        if self._start_tail is not None and self._start_tail.get_parent() is not self:
            self._start_tail = None
        if self._start_tail is None:
            self.prepend(child)
        else:
            self.insert_child_after(child, self._start_tail)
        self._start_tail = child

    def pack_end(self, child, expand=False, _fill=False, padding=0):
        self._layout_child(child, expand, padding)
        if self._start_tail is not None and self._start_tail.get_parent() is not self:
            self._start_tail = None
        if self._start_tail is None:
            self.prepend(child)
        else:
            self.insert_child_after(child, self._start_tail)

    def get_children(self):
        return widget_children(self)


def widget_children(widget):
    result = []
    child = widget.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


def connect_key(widget, callback, capture=False):
    controller = Gtk.EventControllerKey.new()
    controller.set_propagation_phase(
        Gtk.PropagationPhase.CAPTURE if capture else Gtk.PropagationPhase.BUBBLE)
    controller.connect("key-pressed", lambda _controller, keyval, _keycode, state:
                       bool(callback(widget, argparse.Namespace(keyval=keyval, state=state))))
    widget.add_controller(controller)
    return controller


def connect_focus(widget, callback):
    controller = Gtk.EventControllerFocus.new()
    controller.connect("enter", lambda *_: callback(widget, None))
    widget.add_controller(controller)
    return controller


def connect_click(widget, callback, *, capture=False):
    gesture = Gtk.GestureClick.new()
    gesture.set_button(0)
    if capture:
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def pressed(controller, _count, x, y):
        event = argparse.Namespace(button=controller.get_current_button(),
                                   type=Gdk.EventType.BUTTON_PRESS,
                                   state=controller.get_current_event_state(),
                                   x=x, y=y,
                                   gdk_event=controller.get_current_event())
        handled = callback(widget, event)
        if capture and handled:
            controller.set_state(Gtk.EventSequenceState.CLAIMED)

    gesture.connect("pressed", pressed)
    widget.add_controller(gesture)
    return gesture


def on_resize(widget, callback):
    """GTK 4 has no size-allocate signal; watch mapped frame allocations."""
    previous = None

    def tick(current, _clock):
        nonlocal previous
        size = (current.get_allocated_width(), current.get_allocated_height())
        if size != previous:
            previous = size
            callback(current, argparse.Namespace(width=size[0], height=size[1]))
        return True

    widget.add_tick_callback(tick)


def text_has_focus(focus):
    return (focus is not None and
            (isinstance(focus, (Gtk.Entry, Gtk.TextView, Vte.Terminal))
             or focus.get_ancestor(Gtk.Entry) is not None
             or focus.get_ancestor(Gtk.TextView) is not None))


def drawing_overlay(widget, draw):
    overlay = Gtk.Overlay()
    overlay.set_child(widget)
    area = Gtk.DrawingArea()
    area.set_hexpand(True)
    area.set_vexpand(True)
    area.set_can_target(False)
    area.set_draw_func(lambda _area, context, _width, _height: draw(area, context))
    overlay.add_overlay(area)
    overlay.drawing_area = area
    return overlay


def label(text="", css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_selectable(True)
    widget.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda *_: callback())
    return widget


def dialog_button(widget):
    widget.add_css_class("mc-dialog-button")
    widget.set_receives_default(True)
    widget.set_hexpand(True)

    def focus_changed(button, _property):
        if button.has_focus():
            button.add_css_class("mc-dialog-focused")
        else:
            button.remove_css_class("mc-dialog-focused")

    widget.connect("notify::has-focus", focus_changed)
    return widget


def yes_no_dialog(parent, text, secondary=None):
    dialog = Gtk.Window(transient_for=parent, modal=True)
    dialog.set_decorated(False)
    dialog.set_resizable(False)
    dialog.add_css_class("mc-dialog")
    content = Box(orientation=Gtk.Orientation.VERTICAL)
    content.add_css_class("mc-dialog-content")
    content.set_size_request(580, -1)
    dialog.set_child(content)

    title = Gtk.Label(label="CONFIRM", xalign=0)
    title.add_css_class("mc-dialog-title")
    content.append(title)

    message = Gtk.Label(label=text, xalign=0)
    message.set_wrap(True)
    message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    message.set_max_width_chars(44)
    message.add_css_class("mc-dialog-message")
    content.append(message)
    if secondary:
        detail = Gtk.Label(label=secondary, xalign=0)
        detail.set_wrap(True)
        detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail.set_max_width_chars(44)
        detail.add_css_class("mc-dialog-detail")
        content.append(detail)

    actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    actions.add_css_class("mc-dialog-actions")
    no = Gtk.Button(label="_No", use_underline=True)
    yes = Gtk.Button(label="_Yes", use_underline=True)
    for choice in (yes, no):
        dialog_button(choice)
    actions.append(no)
    actions.append(yes)
    content.append(actions)
    dialog.set_default_widget(yes)
    return dialog, yes, no


def confirm(parent, text, secondary, on_yes, on_no=None):
    dialog, yes, no = yes_no_dialog(parent, text, secondary)
    answered = False

    def answer(approved):
        nonlocal answered
        if answered:
            return
        answered = True
        dialog.destroy()
        if approved:
            on_yes()
        elif on_no is not None:
            on_no()

    yes.connect("clicked", lambda *_: answer(True))
    no.connect("clicked", lambda *_: answer(False))
    dialog.connect("close-request", lambda *_: (answer(False), True)[1])

    def dialog_key(_widget, event):
        if event.keyval == Gdk.KEY_Escape:
            answer(False)
            return True
        if event.keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            answer(dialog.get_focus() is not no)
            return True
        return False

    connect_key(dialog, dialog_key, capture=True)
    dialog.present()
    yes.grab_focus()
    return dialog


def scrolled(widget):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_child(widget)
    return scroll


def textview():
    view = Gtk.TextView(editable=False, cursor_visible=False,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(24)
    view.set_right_margin(24)
    view.set_top_margin(20)
    view.set_bottom_margin(20)
    return view
