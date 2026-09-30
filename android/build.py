#!/usr/bin/env python3
"""Build the dependency-free Java Android client with official SDK tools.

Signing files must live outside this source tree. Existing identity is preserved.
No Gradle, remote build service, package publishing, or broker credentials needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent


def run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"Build step failed: {Path(command[0]).name}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-jar", required=True, type=Path)
    parser.add_argument("--build-tools", required=True, type=Path)
    parser.add_argument("--signing-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "Agent_Arena.apk")
    args = parser.parse_args()
    android_jar = args.android_jar.resolve()
    tools = args.build_tools.resolve()
    signing = args.signing_dir.resolve()
    output = args.output.resolve()
    if signing == ROOT or ROOT in signing.parents:
        parser.error("Signing directory must be outside the Android source tree.")
    if not android_jar.is_file():
        parser.error("Android SDK android.jar was not found.")
    for name in ("aapt2", "zipalign", "d8", "apksigner"):
        if not (tools / name).is_file():
            parser.error(f"Missing Android SDK tool: {name}")
    java = shutil.which("java")
    keytool = shutil.which("keytool")
    if not java or not keytool:
        parser.error("Java 17+ with the compiler module and keytool is required.")
    compiler = [java, "-m", "jdk.compiler/com.sun.tools.javac.Main"]
    build = ROOT / "build"
    for sub in ("classes", "generated", "dex", "policy-tests"):
        destination = build / sub
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
    main_src = ROOT / "app" / "src" / "main"

    # Security boundary test has no Android framework dependency and runs on JVM.
    run(compiler + ["-d", str(build / "policy-tests"), str(main_src / "java/com/agentarena/mobile/OriginPolicy.java"), str(ROOT / "tests/OriginPolicyTest.java")])
    policy_result = run([java, "-cp", str(build / "policy-tests"), "com.agentarena.mobile.OriginPolicyTest"])
    print(policy_result.strip())
    back_result = run([sys.executable, str(ROOT / "tests/back_navigation_test.py"), "--java", java])
    print(back_result.strip())

    compiled_resources = build / "resources.zip"
    run([str(tools / "aapt2"), "compile", "--dir", str(main_src / "res"), "-o", str(compiled_resources)])
    resource_apk = build / "resources.apk"
    run([str(tools / "aapt2"), "link", "-I", str(android_jar), "--manifest", str(main_src / "AndroidManifest.xml"), "--java", str(build / "generated"), "-o", str(resource_apk), str(compiled_resources)])
    java_sources = sorted((main_src / "java").rglob("*.java")) + sorted((build / "generated").rglob("*.java"))
    boot_classpath = os.pathsep.join((str(android_jar), str(tools / "core-lambda-stubs.jar")))
    compile_output = run(compiler + ["-source", "8", "-target", "8", "-bootclasspath", boot_classpath, "-encoding", "UTF-8", "-d", str(build / "classes")] + [str(path) for path in java_sources])
    (build / "compiler-output.txt").write_text(compile_output)
    class_files = sorted((build / "classes").rglob("*.class"))
    run([str(tools / "d8"), "--release", "--min-api", "26", "--lib", str(android_jar), "--output", str(build / "dex")] + [str(path) for path in class_files])
    unsigned = build / "unsigned.apk"
    shutil.copy2(resource_apk, unsigned)
    with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for dex in sorted((build / "dex").glob("*.dex")):
            archive.write(dex, dex.name)
    aligned = build / "aligned.apk"
    run([str(tools / "zipalign"), "-P", "16", "-f", "4", str(unsigned), str(aligned)])

    signing.mkdir(parents=True, exist_ok=True)
    signing.chmod(0o700)
    keystore = signing / "agent-arena-release.p12"
    password_file = signing / "keystore-password.txt"
    if keystore.exists() != password_file.exists():
        parser.error("Incomplete signing identity. Restore both the keystore and password; never silently replace an existing key.")
    if not keystore.exists():
        password_file.write_text(secrets.token_urlsafe(40) + "\n")
        password_file.chmod(0o600)
        run([keytool, "-genkeypair", "-keystore", str(keystore), "-storetype", "PKCS12", "-storepass:file", str(password_file), "-keypass:file", str(password_file), "-alias", "agent-arena-release", "-keyalg", "RSA", "-keysize", "3072", "-validity", "10000", "-dname", "CN=Agent Arena Personal Release,OU=Personal App,O=Agent Arena,C=US"])
    keystore.chmod(0o600)
    output.parent.mkdir(parents=True, exist_ok=True)
    # PKCS12 uses the store password for the private key. Reading the same
    # password file twice would advance apksigner's shared file reader to EOF.
    run([str(tools / "apksigner"), "sign", "--ks", str(keystore), "--ks-key-alias", "agent-arena-release", "--ks-pass", "file:" + str(password_file), "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true", "--out", str(output), str(aligned)])
    verification = run([str(tools / "apksigner"), "verify", "--verbose", "--print-certs", str(output)])
    alignment = run([str(tools / "zipalign"), "-c", "-P", "16", "4", str(output)])
    badging = run([str(tools / "aapt"), "dump", "badging", str(output)])
    (build / "apk-verification.txt").write_text(verification + "\n" + alignment + "\n" + badging)
    public_certificate = signing / "agent-arena-release-certificate.pem"
    run([keytool, "-exportcert", "-rfc", "-keystore", str(keystore), "-storepass:file", str(password_file), "-alias", "agent-arena-release", "-file", str(public_certificate)])
    metadata = {
        "application_id": "com.agentarena.mobile", "version": "0.6.1", "version_code": 601,
        "min_sdk": 26, "target_sdk": 35, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "apk_bytes": output.stat().st_size, "security_test": policy_result.strip(),
        "navigation_test": back_result.strip(),
        "build_validation": "Compiled, dexed, zipaligned, signed, and APK signature verified. No device or emulator run claimed.",
    }
    (build / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (signing / "README.txt").write_text(
        "PRIVATE ANDROID SIGNING BACKUP\n\n"
        "This directory contains the private release signing key and its password.\n"
        "Keep the ZIP private and retain it to sign future updates to com.agentarena.mobile.\n"
        "Do not upload it to a public repository, publish it with the app, or embed it in an APK.\n"
        "The ZIP itself is not encrypted. The PKCS12 password is in keystore-password.txt.\n\n"
        "To rebuild, extract this directory outside the source project and pass its path to\n"
        "android/build.py --signing-dir. That script preserves this identity and does not replace it.\n"
        "Alias: agent-arena-release\nStore format: PKCS12\n"
        "The PEM certificate is public; the .p12 and password file are private.\n"
        "Losing the identity can require uninstalling the old APK to install future builds.\n"
    )
    print(json.dumps(metadata, indent=2))
    print("APK written:", output)


if __name__ == "__main__":
    main()
