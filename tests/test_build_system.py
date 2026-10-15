import io
import json
import os
from pathlib import Path
import shlex
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/workflows/scripts"
sys.path.insert(0, str(SCRIPTS))

# Version lookup is unrelated to these offline build-system regressions.
with mock.patch("urllib.request.urlopen") as response, mock.patch("sys.stdout", new_callable=io.StringIO):
    response.return_value.__enter__.return_value.read.return_value = b'#define SUSFS_VERSION "test"'
    import config
    import build
    import matrix_generator
    from kernel_builder import BuildResult, KernelBuilder, ShellCommand
    from extract_artifacts import process_artifacts


class BuildSystemTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory(prefix="gki test ")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.original_cwd)
        self.root = Path(self.tmp.name)
        self.config = config.BuildConfig("android14", "6.1", "124", "2025-02")
        self.builder = KernelBuilder(self.config, str(self.root))

    def executable(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\nset -eu\n" + body)
        path.chmod(0o755)
        return path

    def test_pipeline_failure_preserves_failure_status(self):
        with self.assertRaises(subprocess.CalledProcessError):
            ShellCommand().run("exit 17 | true")

    def test_clone_failure_stops_before_later_dependencies(self):
        with mock.patch.object(self.builder, "_run_cmd", side_effect=subprocess.CalledProcessError(128, "git clone")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                self.builder.clone_repositories()
        self.assertEqual(run.call_count, 1)

    def test_repo_init_failure_is_not_masked_as_sync_failure(self):
        launcher = self.executable("repo", 'exit 17\n')
        self.builder.env["REPO"] = str(launcher)
        # REPO is a path and must also work when the workspace contains spaces.
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.builder.init_and_sync_kernel()
        self.assertEqual(error.exception.returncode, 17)

    def test_failed_launcher_download_is_not_installed(self):
        self.executable("fake-bin/curl", 'exit 22\n')
        self.builder.env["PATH"] = str(self.root / "fake-bin") + os.pathsep + os.environ["PATH"]
        with self.assertRaises(subprocess.CalledProcessError):
            self.builder.setup_repo_tool()
        self.assertFalse((self.root / "git-repo/repo").exists())

    def test_missing_susfs_patch_fails_instead_of_building_without_susfs(self):
        with self.assertRaisesRegex(FileNotFoundError, "SUSFS patch"):
            self.builder.apply_susfs_patches()

    def test_stable_and_dev_select_supported_installer_revisions(self):
        for version, expected in ((config.KSUVersion.STABLE.value, []),
                                  (config.KSUVersion.DEV.value, ["main"])):
            with self.subTest(version=version):
                self.config.kernelsu_version = version
                with mock.patch.object(self.builder, "_run_cmd") as run:
                    self.builder.add_kernelsu()
                args = shlex.split(run.call_args_list[-1].args[0])
                self.assertEqual(args, ["bash", str(self.builder.work_dir / "kernelsu-setup.sh"), *expected])

    def test_cli_and_actions_use_the_same_matrix(self):
        configured = json.loads((SCRIPTS.parent / "config/matrix.json").read_text())
        self.assertEqual(build.DEFAULT_BUILD_MATRIX, configured)
        self.assertEqual(len(matrix_generator.generate_build_matrix()), sum(map(len, configured.values())))

    def test_matrix_retains_susfs_pin(self):
        with mock.patch.object(sys, "argv", ["build.py", "--matrix", "android14-6.1", "--susfs-commit", "HEAD~1", "--dry-run"]):
            args = build.parse_args()
        with mock.patch.object(build, "build_single", side_effect=lambda cfg, *_: BuildResult(True, cfg)) as single:
            results = build.build_matrix(args.matrix, args, str(self.root))
        self.assertEqual(len(results), 5)
        self.assertTrue(all(call.args[0].susfs_commit == "HEAD~1" for call in single.call_args_list))
        self.assertTrue(all(result.config.to_dict()["susfs_commit"] == "HEAD~1" for result in results))

    def test_no_kpm_is_reflected_in_kernel_configuration(self):
        self.config.use_kpm = False
        defconfig = self.builder.work_dir / "common/arch/arm64/configs/gki_defconfig"
        defconfig.parent.mkdir(parents=True)
        defconfig.write_text("CONFIG_64BIT=y\n")
        self.builder.configure_kernel()
        values = [line for line in defconfig.read_text().splitlines() if line.startswith("CONFIG_KPM=")]
        self.assertEqual(values[-1], "CONFIG_KPM=n")

    def test_bazel_runs_distribution_target_into_packaging_directory(self):
        bazel = self.builder.work_dir / "tools/bazel"
        bazel.parent.mkdir()
        bazel.write_text('''#!/bin/bash
set -eu
printf '%s\\n' "$@" > bazel-args.txt
for arg in "$@"; do
  case "$arg" in --dist_dir=*) dist="${arg#--dist_dir=}";; esac
done
mkdir -p "$dist"
printf 'kernel fixture' > "$dist/Image"
''')
        bazel.chmod(0o755)
        self.builder.env["BAZEL_CACHE_DIR"] = str(self.root / "bazel cache")
        self.builder.env["GKI_BUILD_JOBS"] = "2"
        self.assertTrue(self.builder.build_kernel())
        args = (self.builder.work_dir / "bazel-args.txt").read_text().splitlines()
        self.assertEqual(args[0], "run")
        self.assertIn("--jobs=2", args)
        self.assertEqual(args[-1], "--dist_dir=" + str(self.builder.dist_dir))
        self.assertTrue((self.builder.dist_dir / "Image").is_file())

    def test_no_signing_key_still_produces_real_archives_for_all_variants(self):
        self.builder.env.pop("BOOT_SIGN_KEY_PATH", None)
        self.builder.dist_dir.mkdir(parents=True)
        (self.builder.dist_dir / "Image").write_bytes(b"raw kernel fixture")
        (self.builder.dist_dir / "Image.lz4").write_bytes(b"lz4 kernel fixture")
        self.builder.anykernel_dir.mkdir()
        (self.builder.anykernel_dir / "anykernel.sh").write_text("template")
        (self.builder.anykernel_dir / ".git").mkdir()
        (self.builder.anykernel_dir / ".git/config").write_text("must not be packaged")
        self.assertEqual(self.builder.prepare_boot_images(), [])
        expected_payloads = [
            (self.builder.work_dir / name).read_bytes() for name in ("Image", "Image.lz4", "Image.gz")
        ]
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                artifacts = self.builder.create_anykernel_zips()
                self.assertEqual(len(artifacts), 3)
                for path, expected in zip(artifacts, expected_payloads):
                    self.assertEqual(Path(path).parent, self.builder.work_dir)
                    with zipfile.ZipFile(path) as archive:
                        self.assertEqual(archive.read("Image"), expected)
                        self.assertIn("anykernel.sh", archive.namelist())
                        self.assertFalse(any(name.startswith(".git/") for name in archive.namelist()))
        self.assertEqual({path.name for path in self.builder.anykernel_dir.iterdir()}, {".git", "anykernel.sh"})

    def test_missing_kernel_image_cannot_be_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "nonempty Image"):
            self.builder.prepare_boot_images()

    def test_failed_avb_signing_does_not_report_an_unsigned_boot_image(self):
        self.builder.dist_dir.mkdir(parents=True)
        (self.builder.dist_dir / "Image").write_bytes(b"kernel fixture")
        key = self.root / "signing-key.fixture"
        key.write_text("test fixture; not a private key")
        self.builder.env["BOOT_SIGN_KEY_PATH"] = str(key)
        self.builder.env["MKBOOTIMG"] = str(self.executable("mkbootimg", 'printf fixture > boot.img\n'))
        self.builder.env["AVBTOOL"] = str(self.executable("avbtool", 'exit 23\n'))
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.builder.prepare_boot_images()
        self.assertEqual(error.exception.returncode, 23)
        self.assertEqual(list(self.builder.work_dir.glob("android*-boot.img")), [])

    def test_result_files_are_not_merged_into_checksums(self):
        artifacts, results, output = self.root / "artifacts", self.root / "results", self.root / "output"
        artifacts.mkdir()
        results.mkdir()
        checksum = "abc123  android14-6.1.124-2025-02-AnyKernel3.zip\n"
        (results / "android14-6.1-124.txt").write_text(checksum)
        (results / "status-android14-6.1-124.txt").write_text("android14-6.1-124:outcome=success\n")
        process_artifacts(str(artifacts), str(output), str(results))
        self.assertEqual((output / "SHA256SUMS.txt").read_text(), checksum)

    def test_version_lookup_uses_verified_tls(self):
        with mock.patch("urllib.request.urlopen") as response:
            response.return_value.__enter__.return_value.read.return_value = b'#define SUSFS_VERSION "2.3.0"'
            self.assertEqual(config.get_susfs_version(), "2.3.0")
        context = response.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class WorkflowTests(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())

    def test_kernel_smoke_preserves_python_failure_through_tee(self):
        workflow = self.load("build-system-tests.yml")
        step = next(step for step in workflow["jobs"]["kernel-smoke"]["steps"]
                    if step.get("name") == "Compile and package Android 14 kernel")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            python = root / "python"
            python.write_text("#!/bin/bash\nprintf 'build failure fixture\\n'\nexit 17\n")
            python.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "RUNNER_TEMP": str(root)}
            result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 17)
            self.assertIn("build failure fixture", (root / "kernel-build.log").read_text())

    def test_workflow_arguments_preserve_spaces_and_shell_characters(self):
        for name in ("kernel-build.yml", "build-kernels.yml"):
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                capture = bin_dir / "python3"
                capture.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ['ARGS_FILE']).write_text(json.dumps(sys.argv[1:]))\n")
                capture.chmod(0o755)
                step = next(step for step in self.load(name)["jobs"]["build"]["steps"] if step.get("name") == "Build kernel")
                env = {key: "" for key in step["env"]}
                custom_version = 'release candidate $(touch SHOULD_NOT_EXIST); "quoted"'
                env.update(ANDROID_VERSION="android14", KERNEL_VERSION="6.1", SUB_LEVEL="124", OS_PATCH="2025-02",
                           KSU_VERSION="Stable(标准)", SUSFS_COMMIT="HEAD~1", USE_KPM="false", CUSTOM_VERSION=custom_version)
                env = {**os.environ, **env, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "ARGS_FILE": str(root / "args.json")}
                subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                               cwd=ROOT, env=env, check=True)
                args = json.loads((root / "args.json").read_text())
                self.assertIn("--custom-version=" + custom_version, args)
                self.assertIn("--susfs-commit=HEAD~1", args)
                self.assertIn("--no-kpm", args)
                self.assertFalse((SCRIPTS / "SHOULD_NOT_EXIST").exists())

    def test_summary_keeps_results_from_every_matrix_job(self):
        workflow = self.load("build-kernels.yml")
        record = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("name") == "Record build result")
        self.assertIn("status-${{ matrix.config.android }}", record["run"])
        summary = next(step for step in workflow["jobs"]["release"]["steps"] if step.get("name") == "Prepare build summary")
        for states in (("success", "failure", "failure"), ("failure", "failure"), ()):
            with self.subTest(states=states), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                results = root / "build-results"
                results.mkdir()
                for index, state in enumerate(states):
                    (results / f"status-kernel-{index}.txt").write_text(f"kernel-{index}:outcome={state}\n")
                env = {**os.environ, "GITHUB_OUTPUT": str(root / "outputs"), "GITHUB_STEP_SUMMARY": str(root / "summary")}
                subprocess.run(["bash", "-eo", "pipefail", "-c", summary["run"]], cwd=root, env=env, check=True, capture_output=True)
                values = dict(line.split("=", 1) for line in (root / "outputs").read_text().splitlines())
                self.assertEqual(int(values["total"]), len(states))
                self.assertEqual(int(values["success"]), states.count("success"))
                self.assertEqual(int(values["failed"]), states.count("failure"))


if __name__ == "__main__":
    unittest.main()
