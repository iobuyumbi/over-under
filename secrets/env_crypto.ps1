<#
  env_crypto.ps1 -- AES-256-CBC file encrypt/decrypt helper (pure .NET, no installs).

  Usage:
    Encrypt: .\env_crypto.ps1 -Mode encrypt -In .\env_bundle.zip -Out .\env_bundle.zip.enc
    Decrypt: .\env_crypto.ps1 -Mode decrypt -In .\env_bundle.zip.enc -Out .\env_bundle.zip

  The passphrase is taken from the ENV_PASSPHRASE environment variable when set;
  otherwise you are prompted (input is hidden).

  Output format: [16-byte salt][16-byte IV][AES-256-CBC ciphertext]
  Key derivation: PBKDF2-HMAC-SHA1, 200000 iterations, 32-byte key.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('encrypt', 'decrypt')][string]$Mode,
  [Parameter(Mandatory = $true)][string]$In,
  [Parameter(Mandatory = $true)][string]$Out
)

$ErrorActionPreference = 'Stop'
$ITERATIONS = 200000

function Get-Passphrase {
  if ($env:ENV_PASSPHRASE) { return $env:ENV_PASSPHRASE }
  $sec = Read-Host -Prompt 'Passphrase' -AsSecureString
  return [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

function Get-Key([string]$pass, [byte[]]$salt) {
  $kdf = New-Object System.Security.Cryptography.Rfc2898DeriveBytes($pass, $salt, $ITERATIONS)
  return $kdf.GetBytes(32)
}

$pass = Get-Passphrase
if ([string]::IsNullOrEmpty($pass)) { throw 'Empty passphrase. Aborting.' }

$inPath = (Resolve-Path $In).Path
$bytes = [System.IO.File]::ReadAllBytes($inPath)

$aes = [System.Security.Cryptography.Aes]::Create()
$aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
$aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7

if ($Mode -eq 'encrypt') {
  $salt = New-Object byte[] 16
  $iv = New-Object byte[] 16
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($salt)
  $rng.GetBytes($iv)

  $aes.Key = Get-Key $pass $salt
  $aes.IV = $iv
  $enc = $aes.CreateEncryptor()
  $cipher = $enc.TransformFinalBlock($bytes, 0, $bytes.Length)

  $outBytes = New-Object byte[] (16 + 16 + $cipher.Length)
  [Array]::Copy($salt, 0, $outBytes, 0, 16)
  [Array]::Copy($iv, 0, $outBytes, 16, 16)
  [Array]::Copy($cipher, 0, $outBytes, 32, $cipher.Length)
  [System.IO.File]::WriteAllBytes($Out, $outBytes)
  Write-Host "Encrypted $inPath -> $Out"
}
else {
  if ($bytes.Length -lt 33) { throw 'Input is too short to be a valid encrypted file.' }
  $salt = New-Object byte[] 16
  $iv = New-Object byte[] 16
  [Array]::Copy($bytes, 0, $salt, 0, 16)
  [Array]::Copy($bytes, 16, $iv, 0, 16)
  $cipher = New-Object byte[] ($bytes.Length - 32)
  [Array]::Copy($bytes, 32, $cipher, 0, $cipher.Length)

  $aes.Key = Get-Key $pass $salt
  $aes.IV = $iv
  $dec = $aes.CreateDecryptor()
  $plain = $dec.TransformFinalBlock($cipher, 0, $cipher.Length)
  [System.IO.File]::WriteAllBytes($Out, $plain)
  Write-Host "Decrypted $inPath -> $Out"
}
