"""Keep a concurrent workspace switch from redirecting an already selected tab."""
import contextlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
URL_A = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=A"
URL_B = URL_A[:-1] + "B"


class WorkspaceRace(unittest.TestCase):
    def test_other_workspace_cannot_navigate_a_selected_controller_tab(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"SN_CONFIG_DIR": directory, "SN_URL": URL_A}))

            def load(name, url):
                spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/sn.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.set_runtime_url(url)
                return module

            a = load("sn_race_a", URL_A)
            b = load("sn_race_b", URL_B)
            pages = {7: "https://inner.dcs.cloud/notebook/st/A/lab", 8: "about:blank"}
            sessions = {"sA": 7}
            a._save_browser_state(session_id="sA", tab_id=7, task_base="/notebook/st/A/",
                                  project_id="p", workspace_id="A")

            def fake_bsk(*args, session=None, tab_id=None, **kwargs):
                if args == ("session", "list"):
                    return [{"session_id": value} for value in sessions]
                if args == ("session", "start"):
                    sessions["sB"] = 8
                    return {"session_id": "sB"}
                if args == ("tab", "list"):
                    selected = sessions[session]
                    return {"tabs": [{"tab_id": selected, "url": pages[selected], "scope": "agent"}]}
                if args[0] == "navigate":
                    pages[tab_id] = ("https://inner.dcs.cloud/notebook/st/B/lab"
                                     if "workspaceId=B" in args[1] else args[1])
                    return {"tab_id": tab_id, "url": pages[tab_id]}
                raise AssertionError(args)

            def dispatch(op, payload, tab_id, timeout, *args):
                return {"ok": True, "data": {"executed_on": pages[tab_id]}}

            for module in (a, b):
                stack.enter_context(patch.object(module, "bsk", fake_bsk))
                stack.enter_context(patch.object(module, "_probe_tab", return_value=True))
                stack.enter_context(patch.object(module, "_eval_js",
                                                 side_effect=lambda tab_id, *args, **kwargs: pages[tab_id]))
                stack.enter_context(patch.object(module, "_call_js", side_effect=dispatch))

            select_a = a.ensure_tab

            def switch_after_selection():
                selected = select_a()
                other = b.ensure_tab()
                self.assertTrue(pages[other].endswith("/B/lab"))
                return selected

            stack.enter_context(patch.object(a, "ensure_tab", side_effect=switch_after_selection))
            result = a.evaluate_op("run_python", {"code": "print('target A')"})
            self.assertTrue(result["data"]["executed_on"].endswith("/A/lab"))
            self.assertNotEqual(a.ensure_session(), b.ensure_session())


if __name__ == "__main__":
    unittest.main()
