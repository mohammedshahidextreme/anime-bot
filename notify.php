<?php
// sugoix.shop -> GitHub: episodes.json badalte hi bot ko turant jagata hai.
// Ise cPanel Cron se har 1 minute chalana hai.
$FILE  = __DIR__ . '/data/episodes.json';      // apni file ka sahi rasta
$STATE = __DIR__ . '/.episodes.hash';
$TOKEN = 'YAHAN_GITHUB_TOKEN_PASTE_KARO';       // sirf is file me, kahin aur nahi
$REPO  = 'mohammedshahidextreme/anime-bot';

if (!file_exists($FILE)) { exit; }
$h = md5_file($FILE);
if (file_exists($STATE) && trim(file_get_contents($STATE)) === $h) { exit; }

$ch = curl_init("https://api.github.com/repos/$REPO/dispatches");
curl_setopt_array($ch, [
  CURLOPT_POST => true,
  CURLOPT_RETURNTRANSFER => true,
  CURLOPT_TIMEOUT => 20,
  CURLOPT_HTTPHEADER => [
    "Authorization: Bearer $TOKEN",
    "Accept: application/vnd.github+json",
    "User-Agent: sugoix-notify",
    "Content-Type: application/json",
  ],
  CURLOPT_POSTFIELDS => json_encode(["event_type" => "episode-update"]),
]);
curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);
if ($code == 204) { file_put_contents($STATE, $h); }
