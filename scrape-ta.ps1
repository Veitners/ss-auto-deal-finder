$ErrorActionPreference = "Stop"
$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
$dir = "C:\Users\veitn\AppData\Local\Temp\claude\C--Users-veitn-Documents-claude\1e339b03-2487-48e3-b1c0-68fef36650f8\scratchpad"

$listings = Get-Content "$dir\listings.json" -Raw | ConvertFrom-Json
$log = "$dir\scrape_ta_log.txt"
"" | Out-File $log -Encoding utf8

$results = New-Object System.Collections.Generic.List[object]
$i = 0
$ok = 0
$missing = 0
$failed = 0

foreach ($l in $listings) {
  $i++
  $taDate = $null
  try {
    $r = Invoke-WebRequest -Uri $l.url -UserAgent $ua -UseBasicParsing -TimeoutSec 15
    $m = [regex]::Match($r.Content, 'id="tdo_223"[^>]*>\s*([^<\r\n]*)')
    if (-not $m.Success) {
      $m = [regex]::Match($r.Content, 'Tehnisk.\s*apskate:\s*</td>\s*<td[^>]*>\s*([^<\r\n]*)')
    }
    if ($m.Success) {
      $val = $m.Groups[1].Value.Trim()
      $dm = [regex]::Match($val, '^(\d{2})\.(\d{4})$')
      if ($dm.Success) {
        $taDate = "$($dm.Groups[2].Value)-$($dm.Groups[1].Value)"
        $ok++
      } else {
        $missing++
      }
    } else {
      $missing++
    }
  } catch {
    $failed++
    "FAILED $($l.id) : $_" | Out-File $log -Append -Encoding utf8
  }
  $results.Add([PSCustomObject]@{ id = $l.id; taDate = $taDate })
  if ($i % 100 -eq 0) {
    "progress $i/$($listings.Count) ok=$ok missing=$missing failed=$failed" | Out-File $log -Append -Encoding utf8
  }
  Start-Sleep -Milliseconds 250
}

$results | ConvertTo-Json -Compress | Set-Content "$dir\ta.json" -Encoding utf8 -NoNewline
"DONE total=$($listings.Count) ok=$ok missing=$missing failed=$failed" | Out-File $log -Append -Encoding utf8
