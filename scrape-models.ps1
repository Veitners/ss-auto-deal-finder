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

$junk = @("today","today-2","today-5","photo","album","sell","buy","change","-other","transports-rent","car-exchange","spare-parts")

$allModels = @{}
$log = "C:\Users\veitn\AppData\Local\Temp\claude\C--Users-veitn-Documents-claude\1e339b03-2487-48e3-b1c0-68fef36650f8\scratchpad\models_log.txt"
"" | Out-File $log -Encoding utf8

foreach ($b in $brands) {
  $slug = $b.slug
  $url1 = "https://www.ss.lv/lv/transport/cars/$slug/"
  try {
    $r1 = Invoke-WebRequest -Uri $url1 -UserAgent $ua -UseBasicParsing -TimeoutSec 20
  } catch {
    "FAILED $slug : $_" | Out-File $log -Append -Encoding utf8
    continue
  }
  Start-Sleep -Milliseconds 300

  $content = $r1.Content
  $modelisIdx = $content.IndexOf("Modelis:")
  $models = New-Object System.Collections.Generic.List[object]
  if ($modelisIdx -ge 0) {
    $selectStart = $content.IndexOf("<select", $modelisIdx)
    $selectEnd = $content.IndexOf("</select>", $selectStart)
    if ($selectStart -ge 0 -and $selectEnd -ge 0) {
      $block = $content.Substring($selectStart, $selectEnd - $selectStart)
      $modelOpts = [regex]::Matches($block, "<option value=`"/lv/transport/cars/$slug/([a-z0-9\-]+)/`">([^<]+)</option>")
      foreach ($m in $modelOpts) {
        $mslug = $m.Groups[1].Value
        $mlabel = $m.Groups[2].Value
        if ($junk -contains $mslug) { continue }
        $models.Add([PSCustomObject]@{ slug = $mslug; label = $mlabel })
      }
    }
  }
  $allModels[$slug] = $models
  "$slug : models=$($models.Count)" | Out-File $log -Append -Encoding utf8
}

$outDir = "C:\Users\veitn\AppData\Local\Temp\claude\C--Users-veitn-Documents-claude\1e339b03-2487-48e3-b1c0-68fef36650f8\scratchpad"
$allModels | ConvertTo-Json -Depth 5 | Out-File "$outDir\models.json" -Encoding utf8
"DONE" | Out-File $log -Append -Encoding utf8
