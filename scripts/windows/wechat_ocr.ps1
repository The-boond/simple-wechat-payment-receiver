param(
    [string]$ProcessNames = 'Weixin,WeChat',
    [string]$WindowTitlePattern = '收款助手|微信|Weixin|WeChat',
    [string]$NotificationAppPattern = '微信|Weixin|WeChat',
    [switch]$IncludeNotifications,
    [switch]$AllowWindowRestore
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ReceiverWindowCapture {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hwnd, System.Text.StringBuilder value, int maxCount);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
}
'@

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and $_.ReturnType.Name -eq 'Task`1'
    })[0]

function Await-WinRt($operation, [Type]$resultType) {
    $method = $asTaskGeneric.MakeGenericMethod($resultType)
    $task = $method.Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}

$StorageFile = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$RandomStream = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$BitmapDecoder = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$SoftwareBitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$OcrEngine = [Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]
$OcrResult = [Windows.Media.Ocr.OcrResult,Windows.Media.Ocr,ContentType=WindowsRuntime]
$engine = $OcrEngine::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'Windows OCR language engine is unavailable' }

$rows = @()
$allowedNames = @($ProcessNames.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$processes = @(Get-Process -Name $allowedNames -ErrorAction SilentlyContinue)
$processById = @{}
foreach ($process in $processes) { $processById[[uint32]$process.Id] = $process }

function Restore-OriginalWindowState([IntPtr]$hwnd, [bool]$wasVisible, [bool]$wasIconic) {
    if ($wasIconic) { [void][ReceiverWindowCapture]::ShowWindow($hwnd, 6) }
    elseif (-not $wasVisible) { [void][ReceiverWindowCapture]::ShowWindow($hwnd, 0) }
}

if ($IncludeNotifications) {
    try {
        $Listener = [Windows.UI.Notifications.Management.UserNotificationListener,Windows.UI.Notifications,ContentType=WindowsRuntime]
        $Access = [Windows.UI.Notifications.Management.UserNotificationListenerAccessStatus,Windows.UI.Notifications,ContentType=WindowsRuntime]
        $Kinds = [Windows.UI.Notifications.NotificationKinds,Windows.UI.Notifications,ContentType=WindowsRuntime]
        $NotificationList = [System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]]
        $listener = $Listener::Current
        $access = Await-WinRt ($listener.RequestAccessAsync()) $Access
        if ($access -eq $Access::Allowed) {
            $notifications = Await-WinRt ($listener.GetNotificationsAsync($Kinds::Toast)) $NotificationList
            foreach ($notification in @($notifications | Sort-Object CreationTime -Descending | Select-Object -First 20)) {
                $appName = $notification.AppInfo.DisplayInfo.DisplayName
                if ($appName -notmatch $NotificationAppPattern) { continue }
                $parts = @($notification.Notification.Visual.Bindings.GetTextElements() |
                    ForEach-Object { $_.Text } | Where-Object { $_ })
                if ($parts.Count -eq 0) { continue }
                $rows += [pscustomobject]@{
                    hwnd = 0
                    process = 'WindowsNotification'
                    title = $appName
                    text = (($parts + $notification.CreationTime.ToLocalTime().ToString('MM月dd日 HH:mm')) -join "`n")
                    capture_mode = 'allowlisted-notification'
                }
            }
        }
    } catch {
        # Visible-window OCR remains available when notification access is off.
    }
}

$handles = [System.Collections.Generic.List[System.IntPtr]]::new()
$callback = [ReceiverWindowCapture+EnumWindowsProc]{
    param([IntPtr]$hwnd, [IntPtr]$lParam)
    [uint32]$pid = 0
    [void][ReceiverWindowCapture]::GetWindowThreadProcessId($hwnd, [ref]$pid)
    if ($processById.ContainsKey($pid)) { $handles.Add($hwnd) }
    return $true
}
[void][ReceiverWindowCapture]::EnumWindows($callback, [IntPtr]::Zero)

foreach ($hwnd in $handles) {
    if (-not [ReceiverWindowCapture]::IsWindow($hwnd)) { continue }
    [uint32]$pid = 0
    [void][ReceiverWindowCapture]::GetWindowThreadProcessId($hwnd, [ref]$pid)
    $process = $processById[$pid]
    $titleBuilder = [System.Text.StringBuilder]::new(512)
    [void][ReceiverWindowCapture]::GetWindowText($hwnd, $titleBuilder, $titleBuilder.Capacity)
    $title = $titleBuilder.ToString()
    if ($title -notmatch $WindowTitlePattern) { continue }

    $wasVisible = [ReceiverWindowCapture]::IsWindowVisible($hwnd)
    $wasIconic = [ReceiverWindowCapture]::IsIconic($hwnd)
    if ((-not $wasVisible -or $wasIconic) -and -not $AllowWindowRestore) { continue }
    if (-not $wasVisible -or $wasIconic) {
        [void][ReceiverWindowCapture]::ShowWindow($hwnd, 9)
        Start-Sleep -Milliseconds 1200
    }

    $rect = New-Object ReceiverWindowCapture+RECT
    if (-not [ReceiverWindowCapture]::GetWindowRect($hwnd, [ref]$rect)) {
        Restore-OriginalWindowState $hwnd $wasVisible $wasIconic
        continue
    }
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 300 -or $height -lt 200 -or $width -gt 3000 -or $height -gt 2200) {
        Restore-OriginalWindowState $hwnd $wasVisible $wasIconic
        continue
    }

    $bitmap = [System.Drawing.Bitmap]::new($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $hdc = $graphics.GetHdc()
    try { $captured = [ReceiverWindowCapture]::PrintWindow($hwnd, $hdc, 2) }
    finally { $graphics.ReleaseHdc($hdc); $graphics.Dispose() }
    if (-not $captured) {
        $bitmap.Dispose()
        Restore-OriginalWindowState $hwnd $wasVisible $wasIconic
        continue
    }

    $scaled = [System.Drawing.Bitmap]::new($width * 2, $height * 2)
    $draw = [System.Drawing.Graphics]::FromImage($scaled)
    $draw.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $draw.DrawImage($bitmap, 0, 0, $scaled.Width, $scaled.Height)
    $draw.Dispose(); $bitmap.Dispose()
    $temporary = Join-Path $env:TEMP ("wechat-receiver-{0}.png" -f [Guid]::NewGuid().ToString('N'))
    try {
        $scaled.Save($temporary, [System.Drawing.Imaging.ImageFormat]::Png)
        $file = Await-WinRt ($StorageFile::GetFileFromPathAsync($temporary)) $StorageFile
        $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) $RandomStream
        $decoder = Await-WinRt ($BitmapDecoder::CreateAsync($stream)) $BitmapDecoder
        $softwareBitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) $SoftwareBitmapType
        $result = Await-WinRt ($engine.RecognizeAsync($softwareBitmap)) $OcrResult
        $rows += [pscustomobject]@{
            hwnd = [long]$hwnd
            process = $process.ProcessName
            title = $title
            text = $result.Text
            capture_mode = if ($wasIconic -or -not $wasVisible) { 'opt-in-restored-window' } else { 'visible-window' }
        }
        $softwareBitmap.Dispose(); $stream.Dispose()
    } finally {
        $scaled.Dispose()
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Restore-OriginalWindowState $hwnd $wasVisible $wasIconic
    }
}

ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
