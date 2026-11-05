"""Structural guards for the browser application in web.html (there is no DOM test harness)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "web.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split('<script nonce="__NONCE__">', 1)[1].split("</script>", 1)[0]


class RebuiltFrontendTests(unittest.TestCase):
    def test_application_landmarks_and_shared_controls_exist(self):
        wanted = {
            "main", "nav-main", "panel-live", "panel-notes", "panel-memos", "panel-assistant", "panel-settings",
            "panel-dev", "control-dock", "dock-slot-top", "dock-slot-home", "arm-button", "disarm-button",
            "record-start", "record-pause", "record-resume", "record-stop", "record-confirm", "companion",
            "companion-dock", "companion-toggle", "assistant-host", "assistant-input", "assistant-send",
            "assistant-conversations", "notes-list", "note-detail", "share-copy", "summary-editor",
            "transcript-source", "settings-list", "toast-region", "quota-org", "quota-orgs", "protocol-keys",
            "timing-list", "settings-raw",
        }
        found = set(re.findall(r'\bid="([^"]+)"', PAGE))
        self.assertEqual(sorted(wanted - found), [])

    def test_every_literal_dom_target_exists(self):
        found = set(re.findall(r'\bid="([^"]+)"', PAGE))
        referenced = set(re.findall(r"\$\('([^']+)'\)", SCRIPT))
        self.assertEqual(sorted(referenced - found), [])

    def test_views_keep_their_routes(self):
        # Bookmarks such as #notes and #ai_assistant keep working after the rebuild.
        for view in ("live", "notes", "memos", "assistant", "settings", "dev"):
            self.assertIn(f'data-view="{view}"', PAGE)
            self.assertIn(f'data-panel="{view}"', PAGE)
        self.assertIn("hash==='ai_assistant'?'assistant'", SCRIPT)

    def test_record_commands_stay_disabled_while_request_is_pending(self):
        self.assertIn("const commandBusy=state.pending.has('record');", SCRIPT)
        for target in ("record-start", "record-pause", "record-resume", "record-stop"):
            match = re.search(rf"\$\('{target}'\)\.disabled=([^;]+)", SCRIPT)
            self.assertIsNotNone(match, target)
            self.assertIn("commandBusy", match.group(1), target)

    def test_recorder_controls_are_one_set_reachable_from_every_view(self):
        self.assertEqual(PAGE.count('id="record-start"'), 1)
        self.assertIn("const slot=state.view==='live'?$('dock-slot-home'):$('dock-slot-top')", SCRIPT)

    def test_device_writes_ask_for_readiness_instead_of_failing(self):
        self.assertIn("function withReadiness(write)", SCRIPT)
        for write in ("saveGroup(card,group.id,button)", "saveNotification", "saveDeviceName(false)"):
            self.assertRegex(SCRIPT, r"withReadiness\(\(?\)?(=>)?" + re.escape(write))

    def test_status_poll_asks_the_backend_to_leave_out_a_held_settings_catalog(self):
        self.assertIn("params.set('settings_revision',state.settingsRevision)", SCRIPT)
        self.assertIn("if(data.setting_details)state.catalog=", SCRIPT)

    def test_assistant_cancel_shows_only_while_a_request_is_running(self):
        self.assertIn("$('assistant-cancel').hidden=!busy", SCRIPT)
        self.assertIn("$('assistant-send').disabled=state.assistant.busy", SCRIPT)

    def test_protocol_keys_are_dev_only_and_switchable(self):
        self.assertIn("document.body.classList.toggle('show-protocol',on)", SCRIPT)
        self.assertIn("body.show-protocol .protocol{display:revert}", PAGE)
        # Everyday views never render planned or blocked feature placeholders.
        self.assertNotIn("status.features", SCRIPT)
        self.assertNotIn("feature.state", SCRIPT)

    def test_mobile_accessibility_and_offline_baseline(self):
        for needle in ('name="viewport"', "viewport-fit=cover", "100dvh", "prefers-reduced-motion",
                       "(hover:hover) and (pointer:fine)", ":focus-visible", 'class="skip-link"', "env(safe-area-inset-bottom"):
            self.assertIn(needle, PAGE)
        self.assertNotRegex(PAGE, r"user-scalable=no|maximum-scale=1")
        self.assertNotRegex(PAGE, r"<(?:script|link)[^>]+(?:src|href)=\"https?:")

    def test_no_html_injection_or_string_evaluation(self):
        self.assertNotRegex(SCRIPT, r"\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write")
        self.assertNotRegex(SCRIPT, r"\beval\(|new Function\(")


if __name__ == "__main__":
    unittest.main()
