param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$Owner,
    [Parameter(Mandatory=$true)][ValidateSet('Word','Excel','PowerPoint')][string]$Application
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class OfficeWindow {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
}
'@
$processName = @{Word='WINWORD'; Excel='EXCEL'; PowerPoint='POWERPNT'}[$Application]
$existing = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$officeApp = $null
$document = $null
$owned = $false
$result = 1
try {
    $officeApp = New-Object -ComObject "$Application.Application"
    $blank = $null
    if ($Application -eq 'Word') {
        $previousSecurity = $officeApp.AutomationSecurity
        $officeApp.AutomationSecurity = 3
        # Word exposes the window handle on Window, rather than Application.
        $blank = $officeApp.Documents.Add()
        $windowHandle = $officeApp.ActiveWindow.Hwnd
    } else {
        $windowHandle = $officeApp.Hwnd
    }
    [uint32]$officeProcessId = 0
    [void][OfficeWindow]::GetWindowThreadProcessId([IntPtr]$windowHandle, [ref]$officeProcessId)
    if ($officeProcessId -eq 0 -or $existing -contains [int]$officeProcessId) {
        if ($Application -eq 'Word') {
            $blank.Close(0)
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($blank)
            $officeApp.AutomationSecurity = $previousSecurity
        }
        $result = 73
    } else {
        $officeProcess = Get-Process -Id $officeProcessId
        @{pid=$officeProcessId; created=$officeProcess.StartTime.ToFileTimeUtc().ToString()} |
            ConvertTo-Json -Compress | Set-Content -LiteralPath $Owner -Encoding UTF8
        $owned = $true
        if ($null -ne $blank) {
            $blank.Close(0)
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($blank)
            $blank = $null
        }
        $officeApp.AutomationSecurity = 3
        $pdf = [IO.Path]::GetExtension($Output) -eq '.pdf'
        $missing = [Type]::Missing
        switch ($Application) {
            'Word' {
                $officeApp.Visible = $false
                $officeApp.DisplayAlerts = 0
                $officeApp.Options.UpdateLinksAtOpen = $false
                $officeApp.Options.UpdateFieldsAtPrint = $false
                $officeApp.Options.UpdateLinksAtPrint = $false
                $document = $officeApp.Documents.Open($Source, $false, $true, $false,
                    '__ttkgpt_no_password__', '__ttkgpt_no_password__', $false,
                    $missing, $missing, $missing, $missing, $false, $false)
                if ($pdf) { $document.ExportAsFixedFormat($Output, 17) }
                else { $document.SaveAs2($Output, 16) }
            }
            'Excel' {
                $officeApp.Visible = $false
                $officeApp.DisplayAlerts = $false
                $officeApp.EnableEvents = $false
                $officeApp.AskToUpdateLinks = $false
                # UpdateLinks=0 prevents external workbook reference updates.
                $document = $officeApp.Workbooks.Open($Source, 0, $true, 5,
                    'ttkgpt_no_pass', 'ttkgpt_no_pass', $true,
                    2, '', $false, $false, 0, $false, $false, 0)
                if ($pdf) { $document.ExportAsFixedFormat(0, $Output) }
                else { $document.SaveAs($Output, 51) }
            }
            'PowerPoint' {
                $officeApp.DisplayAlerts = 1
                # ReadOnly, Untitled, WithWindow. No window and no UpdateLinks call.
                $document = $officeApp.Presentations.Open($Source, -1, 0, 0)
                foreach ($slide in $document.Slides) {
                    foreach ($shape in $slide.Shapes) {
                        if ($shape.Type -in @(10,11)) { $shape.LinkFormat.AutoUpdate = 1 }
                    }
                }
                if ($pdf) { $document.SaveAs($Output, 32) }
                else { $document.SaveAs($Output, 24) }
            }
        }
        $result = 0
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $result = 1
} finally {
    if ($null -ne $document) {
        try {
            if ($Application -eq 'Word') { $document.Close(0) }
            elseif ($Application -eq 'Excel') { $document.Close($false) }
            else { $document.Close() }
        } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($null -ne $officeApp) {
        if ($owned) { try { $officeApp.Quit() } catch {} }
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($officeApp) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
exit $result
