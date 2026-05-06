"""Wrap a release-please release body with container-image and deploy info.

Triggered from the build workflow after the release tag's image has been
pushed to GHCR. Reads the existing release notes (which release-please
generated when it merged the release PR), then sandwiches them between a
"Container Images" section at the top and an "Updating" section at the
bottom — so a release page shows what was built, how to pull it, what
changed, and how to update a deployment, all in one place.

Env vars:
    GH_TOKEN       — GitHub token (read by `gh` CLI)
    REPO           — owner/name (e.g. "Brennan-VanderLaan/stash")
    GIT_TAG        — tag pushed (e.g. "v0.3.0")
    DRY_RUN=1      — print the rendered body instead of editing the release
"""
import os
import subprocess
import sys
import textwrap


def main() -> int:
    repo = os.environ["REPO"]
    git_tag = os.environ["GIT_TAG"]
    dry_run = os.environ.get("DRY_RUN") == "1"

    if not git_tag.startswith("v"):
        print(f"skip: tag {git_tag!r} is not a release tag (no 'v' prefix)")
        return 0

    version = git_tag.lstrip("v")
    parts = version.split(".")
    if len(parts) < 2:
        print(f"skip: tag {git_tag!r} doesn't look like semver")
        return 0
    minor = ".".join(parts[:2])

    image_path = f"ghcr.io/{repo.lower()}"
    image_name = repo.split("/")[-1]
    package_url = f"https://github.com/{repo}/pkgs/container/{image_name}"

    existing = _gh(["release", "view", git_tag, "--json", "body", "--jq", ".body"])
    if existing is None:
        print(f"skip: no GitHub release exists for tag {git_tag!r}")
        return 0

    # Dedent the template first, THEN interpolate. If we interpolate inside
    # the f-string, the embedded release-please content (which has 0 indent)
    # breaks dedent's common-indent detection and leaves the template indented.
    template = textwrap.dedent("""\
        ## Container Images

        Pull this version with one of:

        ```
        docker pull {image_path}:{version}
        docker pull {image_path}:{minor}
        docker pull {image_path}:latest
        ```

        [All published tags →]({package_url})

        ---

        {existing}

        ---

        ## Updating

        On a deployed instance, click **Check for updates** on the Maintenance
        page — Watchtower pulls the new image. To pin to this exact version,
        edit `STASH_IMAGE` in your `deploy/.env`:

        ```
        STASH_IMAGE={image_path}:{version}
        ```
        """)
    body = template.format(
        image_path=image_path,
        version=version,
        minor=minor,
        package_url=package_url,
        existing=existing.strip(),
    )

    if dry_run:
        print(body)
        return 0

    # gh release edit reads --notes-file, so write to a temp file rather than
    # passing the body as an arg (which would hit shell-arg length limits and
    # backtick issues).
    with open("/tmp/release_body.md", "w") as f:
        f.write(body)
    subprocess.run(
        ["gh", "release", "edit", git_tag, "--notes-file", "/tmp/release_body.md"],
        check=True,
    )
    print(f"updated release {git_tag}")
    return 0


def _gh(args: list[str]) -> str | None:
    """Run gh; return stdout, or None if the call failed (e.g. release missing)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return None
    return result.stdout


if __name__ == "__main__":
    raise SystemExit(main())
