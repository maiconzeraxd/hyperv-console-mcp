[CmdletBinding()]
param([switch]$Server)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$count = 0
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $request = $line | ConvertFrom-Json
    if ($request.operation -eq 'crash') { exit 17 }
    if ($request.operation -eq 'slow') { Start-Sleep -Seconds 10 }
    if ($request.operation -eq 'warning') { Write-Warning 'Synthetic non-protocol output' }
    $count++
    $result = @{
        ok = $true
        result = @{
            operation = $request.operation
            call = $count
            process_id = $PID
        }
    }
    $json = ConvertTo-Json -InputObject $result -Compress
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    [Console]::Out.WriteLine("HVMCP1:$encoded")
    [Console]::Out.Flush()
}
