#!/usr/bin/python3

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
import socket
import threading
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ElementTree

import varlink

import unattended_upgrade
from test.test_base import TestBase


TOP_DIR = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(TOP_DIR, "data")
loader = SourceFileLoader("unattended_upgrade_deferred_control", os.path.join(
    TOP_DIR, "unattended-upgrade-deferred-control"))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
control = module_from_spec(spec)
loader.exec_module(control)


class TestDeferredControl(TestBase):

    def setUp(self):
        super().setUp()
        self.rootdir = self.tempdir
        os.makedirs(os.path.join(self.rootdir, "var/run"))
        self.marker = os.path.join(
            self.rootdir, control.APPLY_ON_SHUTDOWN_FILE.lstrip("/"))
        self.skipped = os.path.join(
            self.rootdir, control.SKIPPED_FILE.lstrip("/"))
        self.authorized = []
        self.allow = True

    def authorize(self, action_id, sock):
        self.authorized.append(action_id)
        self.assertIsInstance(sock, socket.socket)
        if not self.allow:
            raise control.NotAuthorized("denied in test")

    def serve(self):
        """Run the service on a temporary socket, return a client proxy"""
        address = "unix:" + os.path.join(self.tempdir, "control.socket")
        service = control.make_service(DATA_DIR, self.rootdir, self.authorize)
        done = threading.Event()

        def run():
            control.serve(service, address, idle_timeout=0.2)
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        for _ in range(100):
            if os.path.exists(address[5:]):
                break
            done.wait(0.02)
        client = varlink.Client(address=address)
        self.addCleanup(client.cleanup)
        connection = client.open(control.INTERFACE)
        self.addCleanup(connection.close)
        return connection, done

    def test_skip_then_enable_restores_pending_work(self):
        open(self.marker, "w").close()
        connection, _ = self.serve()
        connection.Skip()
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(os.path.exists(self.skipped))
        connection.Enable()
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.skipped))
        self.assertEqual(self.authorized, [
            "com.ubuntu.UnattendedUpgrade.Deferred.Skip",
            "com.ubuntu.UnattendedUpgrade.Deferred.Enable"])

    def test_methods_are_idempotent(self):
        # Without pending work, skip is a no-op and enable invents nothing.
        connection, _ = self.serve()
        for method in ("Skip", "Enable", "Skip", "Enable"):
            with self.subTest(method=method):
                getattr(connection, method)()
                self.assertFalse(os.path.exists(self.marker))
                self.assertFalse(os.path.exists(self.skipped))

    def test_denied_call_changes_nothing(self):
        open(self.marker, "w").close()
        self.allow = False
        connection, _ = self.serve()
        with self.assertRaises(varlink.VarlinkError) as error:
            connection.Skip()
        self.assertEqual(error.exception.error(),
                         "com.ubuntu.UnattendedUpgrade.Deferred.NotAuthorized")
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.skipped))

    def test_filesystem_error_is_reported(self):
        connection, _ = self.serve()
        with patch.object(control.os, "replace",
                          side_effect=PermissionError("denied")):
            with self.assertRaises(varlink.VarlinkError) as error:
                connection.Skip()
        self.assertEqual(error.exception.error(),
                         "com.ubuntu.UnattendedUpgrade.Deferred.Failed")

    def test_service_exits_when_idle(self):
        connection, done = self.serve()
        connection.Skip()
        connection.close()
        self.assertTrue(done.wait(5))

    def test_new_pending_decision_drops_stale_skip(self):
        # An ordinary run that finds nothing pending must stop a later
        # Enable() from bringing back work that no longer exists.
        open(self.marker, "w").close()
        control.skip(self.rootdir)
        unattended_upgrade.update_apply_on_shutdown_marker(self.rootdir, False)
        control.enable(self.rootdir)
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(control.SKIPPED_FILE,
                         unattended_upgrade.APPLY_ON_SHUTDOWN_FILE
                         + unattended_upgrade.SKIPPED_SUFFIX)

    def test_main_needs_root(self):
        with patch.object(control.os, "geteuid", return_value=1000):
            self.assertEqual(control.main(["control"]), 1)


class TestPolkitAuthorize(TestBase):

    def check(self, result=None, error=None):
        authority = Mock()
        if error is not None:
            authority.check_authorization_sync.side_effect = error
        else:
            authority.check_authorization_sync.return_value.\
                get_is_authorized.return_value = result
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        with patch("gi.repository.Polkit.Authority.get_sync",
                   return_value=authority):
            control.polkit_authorize("some.action", a)
        return authority

    def test_authorized_caller_passes(self):
        authority = self.check(result=True)
        subject, action_id, details, flags, cancellable = \
            authority.check_authorization_sync.call_args.args
        self.assertEqual(action_id, "some.action")
        from gi.repository import Polkit
        self.assertEqual(flags,
                         Polkit.CheckAuthorizationFlags.ALLOW_USER_INTERACTION)
        # the peer of a socketpair is this very process
        self.assertEqual(subject.get_uid(), os.getuid())

    def test_denied_caller_is_rejected(self):
        with self.assertRaises(control.NotAuthorized):
            self.check(result=False)

    def test_polkit_failure_denies(self):
        with self.assertRaises(control.NotAuthorized):
            self.check(error=RuntimeError("no polkit"))

    def test_pid_fallback_without_pidfd(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        real = a.getsockopt

        def getsockopt(level, option, *args):
            if option == control.SO_PEERPIDFD:
                raise OSError("not supported")
            return real(level, option, *args)

        sock = Mock(wraps=a)
        sock.getsockopt.side_effect = getsockopt
        subject, pidfd = control.caller_subject(sock)
        self.assertIsNone(pidfd)
        self.assertEqual(subject.get_pid(), os.getpid())


class TestPackaging(TestBase):

    def test_polkit_actions(self):
        policy = ElementTree.parse(os.path.join(
            DATA_DIR, "com.ubuntu.unattended-upgrade.policy"))
        defaults = {action.get("id"): action.find("defaults/allow_active").text
                    for action in policy.getroot().iter("action")}
        self.assertEqual(defaults[control.ACTIONS["Skip"]], "auth_admin_keep")
        self.assertEqual(defaults[control.ACTIONS["Enable"]], "yes")

    def test_interface_matches_methods(self):
        with open(os.path.join(
                DATA_DIR, control.INTERFACE + ".varlink")) as f:
            interface = varlink.Interface(f.read())
        self.assertEqual(interface.name, control.INTERFACE)
        self.assertEqual(set(interface.members) & set(control.ACTIONS),
                         set(control.ACTIONS))

    def test_socket_unit_points_at_the_service_socket(self):
        unit = os.path.join(
            TOP_DIR, "debian",
            "unattended-upgrades.unattended-upgrades-control.socket")
        with open(unit) as f:
            content = f.read()
        self.assertIn("ListenStream=" + control.SOCKET_PATH, content)
        self.assertIn("FileDescriptorName=varlink", content)


if __name__ == "__main__":
    unittest.main()
