#!/usr/bin/env bash
set -euo pipefail
version=${2:-${DUCKDB_VERSION:-1.5.6}}
case "$version" in
  1.4.4|1.5.0|1.5.6) ;;
  *) echo "Unsupported DuckDB version: $version (choose 1.4.4, 1.5.0, or 1.5.6)" >&2; exit 1 ;;
esac
destination=${1:-build/deps/$version}
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) cli=linux-amd64 ;;
  Darwin-arm64) cli=osx-arm64 ;;
  Darwin-x86_64) cli=osx-amd64 ;;
  *) echo 'Supported build hosts: Linux x86-64 and macOS.' >&2; exit 1 ;;
esac
root=$(cd "$(dirname "$0")/.." && pwd)
manifest="$root/scripts/duckdb-checksums.txt"
checksum() {
  awk -v version="$version" -v asset="$1" '$1 == version && $2 == asset {print $3}' "$manifest"
}
cli_asset="duckdb_cli-$cli.zip"
cli_sha=$(checksum "$cli_asset")
[[ $cli_sha =~ ^[0-9a-f]{64}$ ]] || {
  echo "Missing or invalid pinned checksum for DuckDB $version on this host" >&2; exit 1;
}
mkdir -p "$destination"
staging=$(mktemp -d "$destination/.extract.XXXXXX")
part=''
trap 'rm -rf "$staging"; [[ -z $part ]] || rm -f "$part"' EXIT
for asset in "$cli_asset"; do
  expected=$cli_sha
  output=duckdb
  archive="$destination/$asset"
  if [[ ! -f $archive ]]; then
    part=$(mktemp "$destination/.$asset.XXXXXX")
    curl --fail --location --retry 3 --proto '=https' --proto-redir '=https' \
      "https://github.com/duckdb/duckdb/releases/download/v$version/$asset" -o "$part"
    if command -v sha256sum >/dev/null; then downloaded=$(sha256sum "$part" | cut -d ' ' -f1)
    else downloaded=$(shasum -a 256 "$part" | cut -d ' ' -f1); fi
    [[ $downloaded == "$expected" ]] || { echo "Checksum mismatch: $part" >&2; exit 1; }
    mv -f "$part" "$archive"
    part=''
  fi
  if command -v sha256sum >/dev/null; then actual=$(sha256sum "$archive" | cut -d ' ' -f1)
  else actual=$(shasum -a 256 "$archive" | cut -d ' ' -f1); fi
  [[ $actual == "$expected" ]] || { echo "Checksum mismatch: $archive" >&2; exit 1; }
  unzip -oq "$archive" "$output" -d "$staging"
  chmod +x "$staging/$output"
  mv -f "$staging/$output" "$destination/$output"
done
"$destination/duckdb" --version
