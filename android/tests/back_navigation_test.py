#!/usr/bin/env python3
"""Exercise the shipped Back handler on a JVM without an Android device.

The production method is compiled into a narrow Activity/WebView harness. This
checks control flow and whether Back invokes session-clearing code. It does not
claim to test Android lifecycle dispatch, browser cookie storage, or gestures.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def production_handler() -> str:
    source = (ROOT / "app/src/main/java/com/agentarena/mobile/MainActivity.java").read_text()
    start = source.index("@Override public void onBackPressed() {")
    body = source.index("{", start)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


HARNESS = r'''
public final class BackNavigationTest {
    private static int checks = 0;
    private static void check(boolean value, String label) {
        checks++;
        if (!value) throw new AssertionError(label);
    }
    static class Activity {
        int nativeBacks;
        public void onBackPressed() { nativeBacks++; }
    }
    static class WebView {
        final boolean history;
        int webBacks;
        WebView(boolean history) { this.history = history; }
        boolean canGoBack() { return history; }
        void goBack() { webBacks++; }
    }
    static class ArenaActivity extends Activity {
        WebView webView;
        boolean disconnected;
        String savedOrigin = "https://arena.example.com";
        boolean sessionCookie = true;
        int disconnects;
        ArenaActivity(WebView view, boolean isDisconnected) {
            webView = view;
            disconnected = isDisconnected;
        }
        void disconnect() {
            disconnects++;
            savedOrigin = "";
            sessionCookie = false;
            disconnected = true;
        }
        /* PRODUCTION_HANDLER */
    }
    private static void scenario(String label, WebView view, boolean disconnected,
                                 boolean expectWebBack) {
        ArenaActivity activity = new ArenaActivity(view, disconnected);
        activity.onBackPressed();
        check(activity.nativeBacks == (expectWebBack ? 0 : 1), label + ": native back");
        check(view == null || view.webBacks == (expectWebBack ? 1 : 0), label + ": web back");
        check(activity.savedOrigin.equals("https://arena.example.com"), label + ": saved origin");
        check(activity.sessionCookie, label + ": session retained");
        check(activity.disconnects == 0, label + ": no disconnect");
    }
    public static void main(String[] args) {
        scenario("dashboard root", new WebView(false), false, false);
        scenario("page history", new WebView(true), false, true);
        scenario("connection screen", null, true, false);
        scenario("renderer recovery", null, false, false);
        scenario("inactive old history", new WebView(true), true, false);
        System.out.println("BackNavigation: " + checks + " regression checks passed.");
    }
}
'''


def run_tests(java: str) -> str:
    with tempfile.TemporaryDirectory(prefix="arena-back-test-") as directory:
        source = Path(directory) / "BackNavigationTest.java"
        source.write_text(HARNESS.replace("/* PRODUCTION_HANDLER */", production_handler()))
        subprocess.run([java, "-m", "jdk.compiler/com.sun.tools.javac.Main", "-d", directory,
                        str(source)], check=True, capture_output=True, text=True)
        result = subprocess.run([java, "-cp", directory, "BackNavigationTest"], check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", default=shutil.which("java"))
    args = parser.parse_args()
    if not args.java:
        parser.error("Java with the jdk.compiler module is required.")
    print(run_tests(args.java))
