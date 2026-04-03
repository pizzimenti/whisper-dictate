"""Canonical D-Bus contract for the whisper-dictate session service."""

from __future__ import annotations

from whisper_dictate.constants import DBUS_INTERFACE

DBUS_INTROSPECTION_XML = f"""\
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{DBUS_INTERFACE}">
    <method name="Start" />
    <method name="Stop" />
    <method name="Toggle" />
    <method name="GetState">
      <arg direction="out" name="state" type="s" />
    </method>
    <method name="GetLastText">
      <arg direction="out" name="text" type="s" />
    </method>
    <method name="Ping">
      <arg direction="out" name="response" type="s" />
    </method>
    <signal name="StateChanged">
      <arg name="state" type="s" />
    </signal>
    <signal name="PartialTranscript">
      <arg name="text" type="s" />
    </signal>
    <signal name="FinalTranscript">
      <arg name="text" type="s" />
    </signal>
    <signal name="ErrorOccurred">
      <arg name="code" type="s" />
      <arg name="message" type="s" />
    </signal>
  </interface>
</node>
"""
