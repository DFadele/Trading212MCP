# Open-source release readiness

The code and deployment examples are prepared for review, not automatically published. This checkout does not contain Git history/remote configuration, cloud credentials, a public domain or a selected project licence. Creating files here does not prove that CI has run remotely, a cloud deployment exists or a ChatGPT connection has been authorized.

## Licence decision

**Recommendation: MIT** for a small permissive Python integration when simple reuse is the priority. Consider **Apache-2.0** when an explicit contributor patent grant and its accompanying notice obligations are preferred. The owner must confirm rights to the code/contributions and select/add the actual licence and copyright notices before distributing it; no licence is assigned by this task.

Use the standard texts from [MIT](https://opensource.org/license/mit) or [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0), not a custom financial disclaimer as a substitute for a software licence. After choosing, add `LICENSE` and the appropriate `project.license` metadata. Document dependency notices/SBOM separately. Neither licence grants rights to Trading 212 trademarks or vendor financial datasets. Market-data rights remain governed by their providers.

## Publication checklist

1. Select the actual hosting repository, licence, maintainer/contact and supported release policy. Add those real values to project metadata; do not invent organization/repository URLs or report contacts.
2. Review the intended public files and scan complete Git history with a reputable history-aware secrets scanner. This workspace's `.env`, `.env.http`, `.cache`, databases, reports containing real portfolio data and local client/tunnel profiles must remain private. Rotate any previously exposed real credentials.
3. Run the README's pinned checks on a clean checkout. Review the CI workflow: no account/cloud secrets are needed, fork tests get read-only repository permissions, container builds do not publish images, and no `pull_request_target` job executes untrusted code.
4. Build the Docker image on the target architecture. Run its readiness, unauthorized-access and shutdown checks; the CI container job performs build/fail-closed startup and an isolated loopback smoke check. No Docker engine was available in the implementation environment, so do not present the container as locally built until this is done.
5. Configure the actual managed issuer and owner, deploy one instance with HTTPS and managed secrets, and verify the real OAuth login/consent/refresh behaviour, wrong-user rejection and provider connectivity. The signed synthetic-JWT tests and loopback smoke are not an IdP/browser certification.
6. Verify Secure MCP Tunnel and ChatGPT availability/permissions in the intended OpenAI organization/workspace. Keep the plugin private. Do not submit this configured account as a public marketplace service.
7. Review data licensing for the intended deployment, especially any cloud/LLM storage/display/redistribution. Document provider limitations rather than labelling all free sources production-licensed.
8. Pin reviewed GitHub Action/base-image digests for a release, record the resulting application image digest and retain a reproducible release record. Python dependency versions/hashes are committed in `uv.lock`; mutable base/OS package updates are outside that lock. Schedule dependency/security updates and re-run numerical tests.

## Repository structure

- `src/trading212_mcp/quantitative/`: calculations and research workflows (`analytics`, `portfolio`, `risk`, `market_data`, `factors`, `screening`, `candidates`, `optimisation`).
- `src/trading212_mcp/tools/` and `market/`: MCP registration and provider access; deployment modules remain at the package root.
- `runtime_config.py`, `auth.py`, `http.py`, `observability.py`: the deployment boundary; no identity provider or frontend.
- `Dockerfile`, `.dockerignore`, `compose.yaml`: local/portable production image configuration.
- `deploy/render.yaml`: optional managed-host example requiring real configuration and billing review.
- `.env.example`, `.env.http.example`: empty-secret local/HTTP templates.
- `.github/workflows/ci.yml`: Python 3.11/3.12/3.14 runtime tests, Ruff, strict Pyright on 3.14, deployment checks and container build/smoke.
- `SECURITY.md`, `docs/`: security, deployment, financial definitions and known coverage limitations.

Keep the account adapter read-only and scientific calculations independent of deployment/auth code. New users should run separate single-user instances rather than add accounts to this instance.
