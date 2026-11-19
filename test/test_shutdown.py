#!/usr/bin/python3

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
from optparse import Values
import pty
import unittest
from unittest.mock import Mock, patch

import apt_pkg

from test.test_base import TestBase


loader = SourceFileLoader("unattended_upgrade_shutdown", os.path.join(
    os.path.dirname(__file__), "..", "unattended-upgrade-shutdown"))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
shutdown = module_from_spec(spec)
loader.exec_module(shutdown)


class TestShutdown(TestBase):
    def make_helper(self, stop_only=False):
        helper_class = shutdown.UnattendedUpgradesShutdown
        with patch.object(helper_class, "get_inhibit_shutdown_lock"), \
                patch.object(helper_class, "get_inhibit_max_delay", return_value=30):
            helper = helper_class(Values({"delay": 25, "stop_only": stop_only,
                                          "lock_file": "unused"}))
        helper.apt_pkg_reinit_done = True
        return helper

    def test_shutdown_trigger(self):
        for configured, pending, stop_only, stopped, starts in (
                (False, False, False, False, False),
                (False, True, False, False, True),
                (True, False, False, False, True),
                (True, True, True, False, False),
                (False, True, True, False, False),
                (False, True, False, True, False)):
            with self.subTest(configured=configured, pending=pending,
                              stop_only=stop_only, stopped=stopped):
                apt_pkg.config.set("Unattended-Upgrade::InstallOnShutdown",
                                   str(configured))
                helper = self.make_helper(stop_only)
                if stopped:
                    helper.stop_signal_received.set()
                with patch.dict(os.environ), \
                        patch.object(shutdown.os.path, "isfile",
                                     return_value=pending), \
                        patch.object(shutdown.subprocess, "Popen") as popen, \
                        patch.object(shutdown, "log_msg"), \
                        patch.object(shutdown, "log_progress") as progress:
                    popen.return_value.poll.return_value = None
                    self.assertEqual(helper.try_iter_on_shutdown(), starts)
                    self.assertEqual(popen.called, starts)
                    self.assertEqual(progress.called, starts)
                    if starts:
                        self.assertEqual(popen.call_args.args,
                                         (["unattended-upgrade"],))
                        self.assertEqual(popen.call_args.kwargs["env"][
                            "UNATTENDED_UPGRADES_FORCE_INSTALL_ON_SHUTDOWN"], "1")
                        progress.assert_called_once_with(
                            not configured, helper.upgrade_start_time)

    def test_console_and_plymouth_receive_message(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        message = "Installing deferred updates – please wait."
        with patch.object(shutdown.os, "open", return_value=os.dup(slave)) as open_fd, \
                patch.object(shutdown, "do_plymouth") as plymouth, \
                patch.object(shutdown, "do_usplash"):
            shutdown.log_msg(message)
        open_fd.assert_called_once_with(
            "/dev/console", os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        self.assertIn(message, os.read(master, 4096).decode("utf-8"))
        plymouth.assert_called_once_with(message)

    def test_console_unavailable_does_not_prevent_other_messages(self):
        with patch.object(shutdown.os, "open", side_effect=PermissionError), \
                patch.object(shutdown, "do_plymouth") as plymouth, \
                patch.object(shutdown, "do_usplash"):
            shutdown.log_msg("Preparing deferred updates")
        plymouth.assert_called_once_with("Preparing deferred updates")

    def test_blocked_console_write_closes_descriptor(self):
        with patch.object(shutdown.os, "open", return_value=42), \
                patch.object(shutdown.os, "write", side_effect=BlockingIOError), \
                patch.object(shutdown.os, "close") as close_fd, \
                patch.object(shutdown, "do_plymouth") as plymouth, \
                patch.object(shutdown, "do_usplash"):
            shutdown.log_msg("Preparing deferred updates")
        close_fd.assert_called_once_with(42)
        plymouth.assert_called_once_with("Preparing deferred updates")

    def test_missing_display_tools_do_not_prevent_console_message(self):
        for exists in (False, True):
            with self.subTest(exists=exists):
                with patch.object(shutdown.os.path, "exists", return_value=exists), \
                        patch.object(shutdown.subprocess, "call",
                                     side_effect=FileNotFoundError) as call, \
                        patch.object(shutdown, "do_console") as console:
                    shutdown.log_msg("Preparing deferred updates")
                console.assert_called_once_with("Preparing deferred updates")
                self.assertEqual(call.called, exists)

    def test_shutdown_progress_ignores_previous_run(self):
        filename = os.path.join(self.tempdir, "progress")
        for deferred_only in (False, True):
            for state in ("missing", "stale", "empty", "current"):
                with self.subTest(deferred_only=deferred_only, state=state):
                    if state == "missing":
                        if os.path.exists(filename):
                            os.unlink(filename)
                    else:
                        with open(filename, "w") as f:
                            if state != "empty":
                                f.write("Progress: 50 % (test-package)")
                        timestamp = 99 if state == "stale" else 101
                        os.utime(filename, (timestamp, timestamp))
                    with patch.object(shutdown, "PROGRESS_FILE", filename), \
                            patch.object(shutdown, "log_msg") as log:
                        shutdown.log_progress(deferred_only, started=100)
                    message = log.call_args.args[0]
                    if deferred_only:
                        self.assertIn("Applying deferred updates", message)
                        self.assertIn("do not turn off", message)
                    else:
                        self.assertIn("Unattended-upgrade in progress", message)
                        self.assertNotIn("deferred", message.lower())
                    if state == "current":
                        self.assertIn("Progress: 50 % (test-package)", message)
                    else:
                        self.assertNotIn("Progress:", message)

    def test_announcement_only_after_installer_starts(self):
        for configured, expected in (
                (False, "Applying deferred updates during shutdown. "
                        "Please do not turn off the computer."),
                (True, "Running unattended-upgrades in shutdown mode")):
            with self.subTest(configured=configured):
                apt_pkg.config.set("Unattended-Upgrade::InstallOnShutdown",
                                   str(configured))
                helper = self.make_helper()
                with patch.object(shutdown.os.path, "isfile",
                                  return_value=True), \
                        patch.object(shutdown.subprocess, "Popen") as popen, \
                        patch.object(shutdown, "log_msg") as log, \
                        patch.object(shutdown, "log_progress"):
                    popen.return_value.poll.return_value = None
                    self.assertTrue(helper.try_iter_on_shutdown())
                log.assert_called_once_with(expected)

    def test_failed_launch_announces_nothing(self):
        apt_pkg.config.set("Unattended-Upgrade::InstallOnShutdown", "false")
        helper = self.make_helper()
        with patch.object(shutdown.os.path, "isfile", return_value=True), \
                patch.object(shutdown.subprocess, "Popen",
                             side_effect=FileNotFoundError), \
                patch.object(shutdown, "log_msg") as log, \
                patch.object(shutdown, "log_progress") as progress:
            self.assertFalse(helper.try_iter_on_shutdown())
        log.assert_not_called()
        progress.assert_not_called()
        self.assertFalse(helper.on_shutdown_mode)
        self.assertIsNone(helper.on_shutdown_mode_uu_proc)

    def test_shutdown_result_checks_exit_status_and_pending_work(self):
        for deferred_only in (False, True):
            for returncode, pending, success in (
                    (0, False, True), (0, True, False),
                    (1, True, False), (1, False, False), (-15, True, False)):
                with self.subTest(deferred_only=deferred_only, returncode=returncode,
                                  pending=pending):
                    helper = self.make_helper()
                    helper.on_shutdown_mode = True
                    helper.deferred_only = deferred_only
                    helper.on_shutdown_mode_uu_proc = Mock()
                    helper.on_shutdown_mode_uu_proc.poll.return_value = returncode
                    with patch.object(shutdown.os.path, "isfile",
                                      return_value=pending), \
                            patch.object(shutdown, "log_progress") as progress, \
                            patch.object(shutdown, "log_msg") as log, \
                            self.assertRaises(SystemExit) as exit_result:
                        helper.try_iter_on_shutdown()
                    self.assertEqual(exit_result.exception.code, 0 if success else 1)
                    progress.assert_not_called()
                    self.assert_result_message(log.call_args.args[0],
                                               deferred_only, success)

    def assert_result_message(self, message, deferred_only, success):
        if deferred_only:
            self.assertIn("Deferred updates", message)
            self.assertIn("were installed" if success else "remain pending",
                          message)
        elif success:
            self.assertEqual(message, "All upgrades installed")
        else:
            self.assertIn("Unattended-upgrades stopped", message)

    def test_stopped_shutdown_run_reports_child_result(self):
        # After shutdown progressed, the helper asks its own child to stop.
        # It must report the child's real outcome, not "a signal was sent".
        for returncode, pending, success in (
                (0, False, True), (0, True, False), (1, True, False)):
            with self.subTest(returncode=returncode, pending=pending):
                helper = self.make_helper()
                helper.on_shutdown_mode = True
                helper.deferred_only = True
                helper.lock_was_taken = True
                helper.signal_sent = True
                helper.stop_signal_received.set()
                helper.on_shutdown_mode_uu_proc = Mock()
                helper.on_shutdown_mode_uu_proc.wait.return_value = returncode
                with patch.object(shutdown.apt_pkg, "get_lock",
                                  return_value=5), \
                        patch.object(shutdown.os.path, "isfile",
                                     return_value=pending), \
                        patch.object(shutdown, "log_msg") as log, \
                        self.assertRaises(SystemExit) as exit_result:
                    helper.iter()
                self.assertEqual(exit_result.exception.code, 0 if success else 1)
                self.assert_result_message(log.call_args.args[0], True, success)

    def test_stopped_shutdown_run_waits_for_child_exit(self):
        helper = self.make_helper()
        helper.on_shutdown_mode = True
        helper.stop_signal_received.set()
        helper.on_shutdown_mode_uu_proc = Mock()
        helper.on_shutdown_mode_uu_proc.wait.side_effect = \
            shutdown.subprocess.TimeoutExpired("unattended-upgrade", 1)
        with patch.object(shutdown.apt_pkg, "get_lock", return_value=5), \
                patch.object(shutdown.os, "close") as close_fd, \
                patch.object(shutdown, "log_msg") as log:
            self.assertTrue(helper.iter())
        close_fd.assert_called_once_with(5)
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
