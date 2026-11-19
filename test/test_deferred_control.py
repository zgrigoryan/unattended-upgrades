#!/usr/bin/python3

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ElementTree

import unattended_upgrade
from test.test_base import TestBase


TOP_DIR = os.path.join(os.path.dirname(__file__), "..")
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
        patcher = patch.object(control.os, "geteuid", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_control(self, *args):
        return control.main(["unattended-upgrade-deferred-control"] + list(args),
                            self.rootdir)

    def test_skip_then_enable_restores_pending_work(self):
        open(self.marker, "w").close()
        self.assertEqual(self.run_control("skip"), control.EXIT_OK)
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(os.path.exists(self.skipped))

        self.assertEqual(self.run_control("enable"), control.EXIT_OK)
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.skipped))

    def test_commands_are_idempotent(self):
        # Without pending work, skip is a no-op and enable invents nothing.
        for command in ("skip", "enable", "skip", "enable"):
            with self.subTest(command=command):
                self.assertEqual(self.run_control(command), control.EXIT_OK)
                self.assertFalse(os.path.exists(self.marker))
                self.assertFalse(os.path.exists(self.skipped))

    def test_new_pending_decision_drops_stale_skip(self):
        # An ordinary run that finds nothing pending must stop a later
        # "enable" from bringing back work that no longer exists.
        open(self.marker, "w").close()
        self.run_control("skip")
        unattended_upgrade.update_apply_on_shutdown_marker(self.rootdir, False)
        self.assertEqual(self.run_control("enable"), control.EXIT_OK)
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(control.SKIPPED_FILE,
                         unattended_upgrade.APPLY_ON_SHUTDOWN_FILE
                         + unattended_upgrade.SKIPPED_SUFFIX)

    def test_usage_and_root_errors(self):
        self.assertEqual(self.run_control(), control.EXIT_USAGE)
        self.assertEqual(self.run_control("bogus"), control.EXIT_USAGE)
        with patch.object(control.os, "geteuid", return_value=1000):
            open(self.marker, "w").close()
            self.assertEqual(self.run_control("skip"), control.EXIT_ERROR)
        self.assertTrue(os.path.exists(self.marker))

    def test_filesystem_error_is_reported(self):
        with patch.object(control.os, "replace",
                          side_effect=PermissionError("denied")):
            self.assertEqual(self.run_control("skip"), control.EXIT_ERROR)

    def test_polkit_policy(self):
        policy = ElementTree.parse(os.path.join(
            TOP_DIR, "data", "com.ubuntu.unattended-upgrade.deferred.policy"))
        actions = {}
        for action in policy.getroot().iter("action"):
            annotations = {a.get("key"): a.text for a in action.iter("annotate")}
            actions[annotations["org.freedesktop.policykit.exec.argv1"]] = (
                action.find("defaults/allow_active").text,
                annotations["org.freedesktop.policykit.exec.path"])
        path = "/usr/share/unattended-upgrades/unattended-upgrade-deferred-control"
        self.assertEqual(actions, {
            "skip": ("auth_admin_keep", path),
            "enable": ("yes", path),
        })
        self.assertEqual(set(actions), set(control.COMMANDS))


if __name__ == "__main__":
    unittest.main()
