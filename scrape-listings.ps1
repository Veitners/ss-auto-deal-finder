$ErrorActionPreference = "Stop"
$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

$brands = @(
  @{slug="alfa-romeo";name="Alfa Romeo"}, @{slug="audi";name="Audi"}, @{slug="bmw";name="BMW"},
  @{slug="chevrolet";name="Chevrolet"}, @{slug="chrysler";name="Chrysler"}, @{slug="citroen";name="Citroen"},
  @{slug="cupra";name="Cupra"}, @{slug="dacia";name="Dacia"}, @{slug="dodge";name="Dodge"},
  @{slug="fiat";name="Fiat"}, @{slug="ford";name="Ford"}, @{slug="honda";name="Honda"},
  @{slug="hyundai";name="Hyundai"}, @{slug="jaguar";name="Jaguar"}, @{slug="jeep";name="Jeep"},
  @{slug="kia";name="Kia"}, @{slug="lancia";name="Lancia"}, @{slug="land-rover";name="Land Rover"},
  @{slug="lexus";name="Lexus"}, @{slug="mazda";name="Mazda"}, @{slug="mercedes";name="Mercedes"},
  @{slug="mini";name="Mini"}, @{slug="mitsubishi";name="Mitsubishi"}, @{slug="nissan";name="Nissan"},
  @{slug="opel";name="Opel"}, @{slug="peugeot";name="Peugeot"}, @{slug="porsche";name="Porsche"},
  @{slug="renault";name="Renault"}, @{slug="saab";name="Saab"}, @{slug="seat";name="Seat"},
  @{slug="skoda";name="Skoda"}, @{slug="smart";name="Smart"}, @{slug="subaru";name="Subaru"},
  @{slug="suzuki";name="Suzuki"}, @{slug="tesla";name="Tesla"}, @{slug="toyota";name="Toyota"},
  @{slug="volkswagen";name="Volkswagen"}, @{slug="volvo";name="Volvo"}, @{slug="gaz";name="Gaz"},
  @{slug="moskvich";name="Moskvich"}, @{slug="vaz";name="Vaz"}, @{slug="others";name="Citas markas"}
)

$allListings = New-Object System.Collections.Generic.List[object]
$allModels = @{}
$log = "C:\Users\veitn\AppData\Local\Temp\claude\C--Users-veitn-Documents-claude\1e339b03-2487-48e3-b1c0-68fef36650f8\scratchpad\scrape_log.txt"
"" | Out-File $log -Encoding utf8

function Strip-Tags($s) {
  if ($null -eq $s) { return "" }
  $s = [regex]::Replace($s, '<[^>]+>', '')
  return $s.Trim()
}

function Parse-Page($html, $slug) {
  $rows = [regex]::Matches($html, '<tr id="tr_(\d+)">(.*?)</tr>')
  $results = New-Object System.Collections.Generic.List[object]
  foreach ($row in $rows) {
    $id = $row.Groups[1].Value
    $body = $row.Groups[2].Value
    $hrefMatch = [regex]::Match($body, "/msg/lv/transport/cars/$slug/([a-z0-9\-]+)/([a-zA-Z0-9]+)\.html")
    if (-not $hrefMatch.Success) { continue }
    $modelSlug = $hrefMatch.Groups[1].Value
    $adKey = $hrefMatch.Groups[2].Value
    $adUrl = "https://www.ss.lv/msg/lv/transport/cars/$slug/$modelSlug/$adKey.html"
    $titleMatch = [regex]::Match($body, 'id="dm_\d+"[^>]*>(.*?)</a>')
    $title = Strip-Tags $titleMatch.Groups[1].Value
    $oCols = [regex]::Matches($body, 'class="msga2-o pp6" nowrap c=1>(.*?)</td>')
    $rCols = [regex]::Matches($body, 'class="msga2-r pp6" nowrap c=1>(.*?)</td>')
    if ($oCols.Count -lt 4) { continue }
    $modelLabel = Strip-Tags $oCols[0].Groups[1].Value
    $year = Strip-Tags $oCols[1].Groups[1].Value
    $engine = Strip-Tags $oCols[2].Groups[1].Value
    $priceRaw = Strip-Tags $oCols[3].Groups[1].Value
    $mileageRaw = if ($rCols.Count -gt 0) { Strip-Tags $rCols[0].Groups[1].Value } else { "" }

    $priceNum = ($priceRaw -replace '[^\d]', '')
    $yearNum = 0
    [int]::TryParse($year, [ref]$yearNum) | Out-Null
    if ($priceNum -eq "" -or $yearNum -eq 0) { continue }

    $engMatch = [regex]::Match($engine, '^([\d\.]+)([A-Z]?)$')
    $engVol = $null; $engFuel = "petrol"
    if ($engMatch.Success) {
      $engVol = $engMatch.Groups[1].Value
      $code = $engMatch.Groups[2].Value
      $engFuel = switch ($code) {
        "D" { "diesel" }
        "H" { "hybrid" }
        "E" { "electric" }
        "G" { "gas" }
        default { "petrol" }
      }
    }
    $mileageNum = 0
    $mMatch = [regex]::Match($mileageRaw, '([\d\s]+)')
    if ($mMatch.Success) {
      $mileageNum = [int]($mMatch.Groups[1].Value -replace '\s', '') * 1000
    }

    $results.Add([PSCustomObject]@{
      id = $id
      brand = $slug
      model = $modelSlug
      modelLabel = $modelLabel
      title = $title
      year = $yearNum
      engineVolume = $engVol
      fuel = $engFuel
      mileageKm = $mileageNum
      priceEur = [int]$priceNum
      url = $adUrl
    })
  }
  return $results
}

$totalReq = 0
foreach ($b in $brands) {
  $slug = $b.slug
  $url1 = "https://www.ss.lv/lv/transport/cars/$slug/"
  try {
    $r1 = Invoke-WebRequest -Uri $url1 -UserAgent $ua -UseBasicParsing -TimeoutSec 20
  } catch {
    "FAILED page1 $slug : $_" | Out-File $log -Append -Encoding utf8
    continue
  }
  $totalReq++
  Start-Sleep -Milliseconds 300

  # page cap based on pagination
  $pageNums = [regex]::Matches($r1.Content, "cars/$slug/page(\d+)\.html") | ForEach-Object { [int]$_.Groups[1].Value }
  $maxPage = if ($pageNums.Count -gt 0) { ($pageNums | Measure-Object -Maximum).Maximum } else { 1 }

  $pagesToFetch = 1
  if ($maxPage -ge 60) { $pagesToFetch = 6 }
  elseif ($maxPage -ge 15) { $pagesToFetch = 4 }
  elseif ($maxPage -ge 4) { $pagesToFetch = 2 }
  else { $pagesToFetch = $maxPage }
  if ($pagesToFetch -lt 1) { $pagesToFetch = 1 }

  $pageResults = Parse-Page $r1.Content $slug
  foreach ($item in $pageResults) { $allListings.Add($item) }

  for ($p = 2; $p -le $pagesToFetch; $p++) {
    $url = "https://www.ss.lv/lv/transport/cars/$slug/page$p.html"
    try {
      $rp = Invoke-WebRequest -Uri $url -UserAgent $ua -UseBasicParsing -TimeoutSec 20
      $totalReq++
      Start-Sleep -Milliseconds 300
      $pr = Parse-Page $rp.Content $slug
      foreach ($item in $pr) { $allListings.Add($item) }
    } catch {
      "FAILED page$p $slug : $_" | Out-File $log -Append -Encoding utf8
    }
  }

  "$slug : maxPage=$maxPage fetched=$pagesToFetch listingsSoFar=$($allListings.Count) totalReq=$totalReq" | Out-File $log -Append -Encoding utf8
}

$outDir = "C:\Users\veitn\AppData\Local\Temp\claude\C--Users-veitn-Documents-claude\1e339b03-2487-48e3-b1c0-68fef36650f8\scratchpad"
$allListings | ConvertTo-Json -Depth 5 | Out-File "$outDir\listings.json" -Encoding utf8
"DONE totalReq=$totalReq totalListings=$($allListings.Count)" | Out-File $log -Append -Encoding utf8
