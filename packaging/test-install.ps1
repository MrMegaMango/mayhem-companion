[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$ReportPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# This test installs and removes software. Keep it on a disposable CI runner.
if ($env:GITHUB_ACTIONS -ne 'true') {
    throw 'The installer smoke test must run on a disposable GitHub Actions runner.'
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The installer smoke test requires Windows.'
}

$installer = (Resolve-Path -LiteralPath $InstallerPath).ProviderPath
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw 'InstallerPath must identify an installer executable.'
}
if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $ReportPath = Join-Path $env:RUNNER_TEMP 'mayhem-installer-smoke.json'
}
$reportFile = [IO.Path]::GetFullPath($ReportPath)
$reportDirectory = [IO.Path]::GetDirectoryName($reportFile)
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
if (Test-Path -LiteralPath $reportFile) {
    # Never accept a report left by an earlier invocation.
    Remove-Item -LiteralPath $reportFile
}
$installLog = [IO.Path]::ChangeExtension($reportFile, '.install.log')
$uninstallLog = [IO.Path]::ChangeExtension($reportFile, '.uninstall.log')

$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$runName = 'MayhemCompanion-InstallerSmoke-' + [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path $tempBase $runName
$installDirectory = Join-Path $testRoot 'app'
$executable = Join-Path $installDirectory 'MayhemCompanion.exe'
$failures = [Collections.Generic.List[string]]::new()

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $setupArguments = @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
        '/NOICONS', '/TASKS=""', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
        ('/DIR="{0}"' -f $installDirectory),
        ('/LOG="{0}"' -f $installLog)
    )
    $setup = Start-Process -FilePath $installer -ArgumentList $setupArguments `
        -WindowStyle Hidden -PassThru -Wait
    if ($setup.ExitCode -ne 0) {
        throw "Silent installation failed with exit code $($setup.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw 'The installer did not create MayhemCompanion.exe in the requested directory.'
    }

    $smoke = Start-Process -FilePath $executable `
        -ArgumentList @('--self-test', '--report', ('"{0}"' -f $reportFile)) `
        -WindowStyle Hidden -PassThru -Wait
    if (-not (Test-Path -LiteralPath $reportFile -PathType Leaf)) {
        throw "The installed self-test returned $($smoke.ExitCode) without a JSON report."
    }
    $report = Get-Content -LiteralPath $reportFile -Raw | ConvertFrom-Json
    if ($report.schema_version -ne 1 -or $report.frozen -isnot [bool] -or -not $report.frozen) {
        throw 'The self-test report is not a supported frozen-runtime report.'
    }
    if ($report.exit_code -ne $smoke.ExitCode) {
        throw 'The self-test report does not match the process exit code.'
    }
    $requiredChecks = @('tk', 'arena_reference', 'analytics_configuration', 'tray_backend', 'ocr')
    $checkNames = @($report.checks.PSObject.Properties.Name)
    foreach ($name in $requiredChecks) {
        if ($name -notin $checkNames) {
            throw "The self-test report is missing the $name check."
        }
    }

    if ($smoke.ExitCode -eq 0) {
        if ($report.status -ne 'passed') {
            throw 'The self-test exited successfully without an overall passing report.'
        }
        foreach ($check in $report.checks.PSObject.Properties) {
            if ($check.Value.status -ne 'passed') {
                throw "The $($check.Name) runtime check did not pass."
            }
        }
        Write-Host 'Installed runtime passed all checks, including English OCR.'
    }
    elseif ($smoke.ExitCode -eq 2) {
        $ocr = $report.checks.ocr
        if ($report.status -ne 'host_requirement_missing' -or
            $ocr.status -ne 'host_requirement_missing' -or
            $ocr.code -ne 'english_ocr_not_installed') {
            throw 'Exit 2 is allowed only for the explicitly reported missing English OCR capability.'
        }
        foreach ($check in $report.checks.PSObject.Properties) {
            if ($check.Name -ne 'ocr' -and $check.Value.status -ne 'passed') {
                throw "The $($check.Name) runtime check did not pass."
            }
        }
        Write-Host 'Installed runtime passed; the CI host lacks the Windows English OCR capability.'
    }
    else {
        throw "The installed runtime self-test failed with exit code $($smoke.ExitCode)."
    }
}
catch {
    $failures.Add($_.Exception.Message)
}
finally {
    try {
        if (Test-Path -LiteralPath $installDirectory -PathType Container) {
            $uninstallers = @(Get-ChildItem -LiteralPath $installDirectory -Filter 'unins*.exe' -File)
            if ($uninstallers.Count -ne 1) {
                throw "Expected one uninstaller; found $($uninstallers.Count)."
            }
            $uninstall = Start-Process -FilePath $uninstallers[0].FullName `
                -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                    ('/LOG="{0}"' -f $uninstallLog)) `
                -WindowStyle Hidden -PassThru -Wait
            if ($uninstall.ExitCode -ne 0) {
                throw "Silent uninstall failed with exit code $($uninstall.ExitCode)."
            }
            if (Test-Path -LiteralPath $executable) {
                throw 'Uninstall left MayhemCompanion.exe behind.'
            }
            Write-Host 'Uninstall removed the installed executable.'
        }
    }
    catch {
        $failures.Add('Uninstall: ' + $_.Exception.Message)
    }

    # Keep a failed installation available for CI diagnosis. On success, validate
    # the exact temporary root before removing any remaining empty directories.
    if ($failures.Count -eq 0 -and (Test-Path -LiteralPath $testRoot)) {
        $resolvedRoot = (Resolve-Path -LiteralPath $testRoot).ProviderPath.TrimEnd('\')
        $expectedRoot = [IO.Path]::GetFullPath((Join-Path $tempBase $runName)).TrimEnd('\')
        if ($resolvedRoot -ne $expectedRoot -or
            [IO.Path]::GetDirectoryName($resolvedRoot) -ne $tempBase -or
            [IO.Path]::GetFileName($resolvedRoot) -notmatch '^MayhemCompanion-InstallerSmoke-[0-9a-f]{32}$') {
            throw 'Refusing to remove an unexpected temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}

if ($failures.Count -gt 0) {
    throw ($failures -join [Environment]::NewLine)
}
Write-Host "Installer smoke test passed. Report: $reportFile"
