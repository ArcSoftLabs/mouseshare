#!/usr/bin/env bash
set -euo pipefail

readonly APPIMAGETOOL_VERSION="1.9.1"
readonly APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
readonly APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"
# AppImage/type2-runtime release 20251108, fetched and hashed 2026-09-04.
readonly RUNTIME_VERSION="20251108"
readonly RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
readonly RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/${RUNTIME_VERSION}/runtime-x86_64"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DIST="${ROOT}/dist"
readonly TOOL="${DIST}/appimagetool-x86_64.AppImage"
readonly RUNTIME="${DIST}/runtime-x86_64"

mkdir -p "${DIST}"
if [[ ! -f "${TOOL}" ]] || ! echo "${APPIMAGETOOL_SHA256}  ${TOOL}" | sha256sum --check --status; then
    curl --fail --location --silent --show-error "${APPIMAGETOOL_URL}" --output "${TOOL}"
fi
echo "${APPIMAGETOOL_SHA256}  ${TOOL}" | sha256sum --check
chmod +x "${TOOL}"
if [[ ! -f "${RUNTIME}" ]] || ! echo "${RUNTIME_SHA256}  ${RUNTIME}" | sha256sum --check --status; then
    curl --fail --location --silent --show-error "${RUNTIME_URL}" --output "${RUNTIME}"
fi
echo "${RUNTIME_SHA256}  ${RUNTIME}" | sha256sum --check

appdir="$(mktemp -d "${DIST}/MouseShare.AppDir.XXXXXX")"
trap 'rm -rf "${appdir}"' EXIT
mkdir -p "${appdir}/usr/bin" "${appdir}/usr/share/applications" \
    "${appdir}/usr/share/icons/hicolor/256x256/apps"
install -m 0755 "${DIST}/mouseshare" "${appdir}/usr/bin/mouseshare"
install -m 0644 "${ROOT}/packaging/linux/mouseshare.desktop" \
    "${appdir}/usr/share/applications/mouseshare.desktop"
install -m 0644 "${ROOT}/packaging/icons/MouseShare.png" \
    "${appdir}/usr/share/icons/hicolor/256x256/apps/mouseshare.png"
cp "${ROOT}/packaging/linux/mouseshare.desktop" "${appdir}/mouseshare.desktop"
cp "${ROOT}/packaging/icons/MouseShare.png" "${appdir}/mouseshare.png"
ln -s usr/bin/mouseshare "${appdir}/AppRun"

APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "${TOOL}" \
    --runtime-file "${RUNTIME}" \
    "${appdir}" "${DIST}/MouseShare-x86_64.AppImage"
