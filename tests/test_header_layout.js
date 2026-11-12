const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('web/index.html', 'utf8');
const rule = selector => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = html.match(new RegExp(escaped + '\\s*\\{([^}]+)\\}'));
  assert.ok(match, `missing header rule ${selector}`);
  return match[1];
};
assert.match(rule('.top > .ctrl'), /flex-basis:\s*100%/, 'header controls always occupy their own row');
assert.match(rule('.top > .tabs'), /flex-wrap:\s*wrap/, 'navigation can wrap on narrow screens');
assert.match(rule('.top > .tabs'), /min-width:\s*0/, 'navigation can shrink within the header');
console.log('Header controls have a consistent row across all sections');
