const fs = require('node:fs');
const path = require('node:path');
const fragment = fs.readFileSync(path.join(__dirname, 'havenbox-v05.fragment.html'), 'utf8');
const document = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>Havenbox · V0.5 可点击原型</title>
<style>
:root { color-scheme: light dark; background: light-dark(#f6f7f4, #171c19); }
body { margin: 0; padding: 16px; }
#havenbox-v05 { max-width: 1440px; margin: 0 auto; }
@media (max-width: 560px) { body { padding: 8px; } }
</style>
</head>
<body>
${fragment}
</body>
</html>
`;
fs.writeFileSync(path.join(__dirname, 'index.html'), document);
console.log('Built doc/prototypes/index.html (no external resources).');
