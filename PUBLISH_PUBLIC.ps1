param([string]$PublicRepoName = 'CityNexus-Coursework-2026')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$owner = 'Canshu-King'
$sourceRepo = 'https://github.com/Canshu-King/CityNexus.git'
$branch = 'publication/teacher-public-ready-20261106'
foreach ($command in @('git', 'gh')) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Install $command first." }
}
gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }
gh auth setup-git
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI Git credentials setup failed.' }
$dir = Join-Path ([System.IO.Path]::GetTempPath()) ('citynexus-public-' + [guid]::NewGuid().ToString('N'))
git clone --depth 1 --single-branch --branch $branch $sourceRepo $dir
if ($LASTEXITCODE -ne 0) { throw 'Unable to clone staging branch.' }
Push-Location $dir
try {
  $tracked = @(git ls-files)
  # Block actual credential/container filenames, not source modules such as
  # security/credential_literals.py or test_*_credential_gate.py.
  # Block actual secret files without rejecting credential-check source code.
  $bad = @($tracked | Where-Object {
    $baseName = [IO.Path]::GetFileName($_)
    ($baseName -match '(?i)^\.env($|\.)' -and $baseName -notmatch '(?i)^\.env\.example$') -or
    ($baseName -match '(?i)\.(pem|key|p12|pfx|sqlite|sqlite3|db)$') -or
    ($baseName -match '(?i)^(credentials?|service[-_]account|secrets?)\.(json|ya?ml|toml|ini|env|txt)$') -or
    ($baseName -match '(?i)^id_(rsa|ed25519|ecdsa)$')
  })
  if ($bad.Count -gt 0) { throw ('Sensitive filenames detected: ' + ($bad -join '; ')) }
  $pattern = '(?i)(github_pat_[A-Za-z0-9_]{25,}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----)'
  $hits = @()
  foreach ($f in $tracked) {
    if ($f -match '(?i)\.(md|txt|py|js|jsx|ts|tsx|json|yaml|yml|css|html|toml|ps1|sh|svg|xml)$') {
      $item = Get-Item -LiteralPath $f -ErrorAction SilentlyContinue
      if ($item -and $item.Length -lt 2000000) {
        try { if ([IO.File]::ReadAllText($item.FullName) -match $pattern) { $hits += $f } } catch {}
      }
    }
  }
  if ($hits.Count -gt 0) { throw ('Potential secret patterns detected: ' + ($hits -join '; ')) }
  if (-not (Get-Command gitleaks -ErrorAction SilentlyContinue)) {
    throw 'Gitleaks is required for public release. Install with: winget install --id Gitleaks.Gitleaks -e'
  }
  # Redact exact Gitleaks matches ONLY in the isolated public clone.
  # Store reports outside the repo to avoid accidentally publishing the original findings.
  $redactionCount = 0
  $omitted = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  $touched = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  $root = [IO.Path]::GetFullPath($dir).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  $utf8 = [Text.UTF8Encoding]::new($false)
  for ($round = 1; $round -le 4; $round++) {
    $reportFile = Join-Path ([IO.Path]::GetTempPath()) ('cnx-gitleaks-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
      & gitleaks dir . --no-banner --report-format json --report-path $reportFile
      $scanCode = $LASTEXITCODE
      if ($scanCode -eq 0) { break }
      if ($scanCode -ne 1) { throw ('Gitleaks scanner error (exit ' + $scanCode + '). Nothing published.') }
      if (-not (Test-Path -LiteralPath $reportFile)) { throw 'Missing Gitleaks report. Nothing published.' }
      $findings = @((Get-Content -LiteralPath $reportFile -Raw -Encoding UTF8 | ConvertFrom-Json))
      if ($findings.Count -eq 0) { throw 'Gitleaks flagged findings but returned an empty report.' }
      $roundChanged = 0
      foreach ($finding in $findings) {
        $relative = [string]$finding.File
        $secret = [string]$finding.Secret
        if ([string]::IsNullOrWhiteSpace($relative)) { throw 'Gitleaks finding has no file path. Nothing published.' }
        $absolute = [IO.Path]::GetFullPath((Join-Path $dir $relative))
        if (-not $absolute.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
          throw 'Gitleaks reported a path outside the temporary public snapshot.'
        }
        if (-not (Test-Path -LiteralPath $absolute -PathType Leaf)) { continue }
        # Unsafe or unusually short exact matches cannot be reliably replaced.
        # Omit the offending file from the public copy instead of suppressing the finding.
        if ([string]::IsNullOrEmpty($secret) -or $secret.Length -lt 12) {
          Remove-Item -LiteralPath $absolute -Force
          [void]$omitted.Add($relative)
          $roundChanged++
          continue
        }
        try {
          $original = [IO.File]::ReadAllText($absolute, [Text.Encoding]::UTF8)
          if (-not $original.Contains($secret)) {
            Remove-Item -LiteralPath $absolute -Force
            [void]$omitted.Add($relative)
            $roundChanged++
            continue
          }
          $changed = $original.Replace($secret, 'REDACTED_FOR_PUBLIC_RELEASE')
          [IO.File]::WriteAllText($absolute, $changed, $utf8)
          [void]$touched.Add($relative)
          $redactionCount++
          $roundChanged++
        } catch {
          throw ('Could not safely redact ' + $relative + '. Nothing published.')
        }
      }
      if ($roundChanged -eq 0) { throw 'No progress in redacting findings; nothing published.' }
    } finally {
      Remove-Item -LiteralPath $reportFile -Force -ErrorAction SilentlyContinue
    }
  }
  # The final explicit scan is mandatory, including after the maximum rounds.
  & gitleaks dir . --no-banner
  if ($LASTEXITCODE -ne 0) { throw 'Public clone still contains possible secrets; NOTHING published.' }
  $summary = @(
    '# Automated public-release redaction summary',
    '',
    'This is a curated public copy. Exact Gitleaks matches were replaced only in this temporary publication copy.',
    'Redacted matches (number of operations): ' + $redactionCount,
    'Source files changed: ' + $touched.Count,
    'Files omitted because a safe exact replacement was not possible: ' + $omitted.Count,
    '',
    'Redaction can alter source examples and tests. This export was NOT freshly verified to build or run after redaction.',
    'Keep the original private source repository for unredacted development files.',
    ''
  ) -join [Environment]::NewLine
  [IO.File]::WriteAllText((Join-Path $dir 'PUBLIC_REDACTION_SUMMARY.md'), $summary, $utf8)
  Write-Host ('Public clone sanitized: ' + $redactionCount + ' redaction operations, ' + $touched.Count + ' files touched, ' + $omitted.Count + ' files omitted.')
  if ($omitted.Count -gt 0) { Write-Warning ('Omitted files: ' + (($omitted | Sort-Object) -join '; ')) }
  Write-Warning 'Manually check binary PDF/Visio/Project metadata, student details, licenses, code literals and test fixtures before publishing.'
  $ok = Read-Host 'After reviewing files, type PUBLIC to publish a NEW public repository'
  if ($ok -cne 'PUBLIC') { throw 'Cancelled. Nothing was made public.' }
  # gh repo view exits nonzero for a non-existent repository; PowerShell can treat
  # its stderr as a terminating error even when redirecting output.
  # Let gh repo create enforce "must not already exist": it refuses to overwrite.
  Write-Host ('Creating NEW public repository ' + $owner + '/' + $PublicRepoName + ' (existing repositories will not be overwritten).')
  git checkout --orphan public-root
  if ($LASTEXITCODE -ne 0) { throw 'Could not create a history-free commit.' }
  git add -A
  git -c user.name='CityNexus Coursework Team' -c user.email='coursework@users.noreply.github.com' commit -m 'Publish curated CityNexus coursework and implementation evidence'
  if ($LASTEXITCODE -ne 0) { throw 'Root commit failed.' }
  git branch -M main
  gh repo create "$owner/$PublicRepoName" --public --source . --remote public --push --description 'CityNexus Smart City Traveler System coursework and implementation'
  if ($LASTEXITCODE -ne 0) { throw 'GitHub public-repo creation failed. Check remote status before retrying.' }
  Write-Host "https://github.com/$owner/$PublicRepoName"
} finally { Pop-Location }
