// Browser smoke test for the demo and live pages.
//
//   pip install -r requirements.txt && OPERATOR_KEY=test-key python3 app.py   # in one terminal
//   npm install playwright && npx playwright install chromium               # once
//   OPERATOR_KEY=test-key BASE_URL=http://127.0.0.1:5000 node tests/e2e/smoke.js
//
// Exits non-zero on the first failed check.
const { chromium } = require('playwright');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:5000';
const KEY = process.env.OPERATOR_KEY || '';
let failures = 0;

function check(ok, what) {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`);
  if (!ok) failures++;
}

async function newPage(browser, opts = {}) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage();
  page.errors = [];
  page.on('pageerror', e => page.errors.push(e.message));
  page.on('console', m => {
    // Expected: 403 from the deliberate wrong-code check; vibrate needs a user tap in headless runs.
    if (m.type() === 'error' && !/403|vibrate/.test(m.text())) page.errors.push(m.text());
  });
  return page;
}

async function carStatuses(page) {
  return page.$$eval('ul.cars li', lis => lis.map(li => li.querySelector('.st').textContent.trim()));
}

async function demo(browser) {
  const page = await newPage(browser, { viewport: { width: 1280, height: 800 } });
  await page.goto(`${BASE}/demo`);
  await page.waitForFunction(() => document.querySelectorAll('ul.cars li').length === 3);
  await page.waitForTimeout(1500);

  // Starting cars: A behind, B off the route, C ahead.
  const st = await carStatuses(page);
  check(st[0] === 'no alert', `demo: car behind the ambulance is not alerted (${st[0]})`);
  check(st[1] === 'no alert', `demo: car 280 m off the route is not alerted (${st[1]})`);
  check(st[2] === 'ALERT', `demo: car on the route ahead is alerted (${st[2]})`);
  check(await page.locator('#screen .banner').isVisible(), 'demo: selected car shows the alert banner');

  // Clicking the map adds a car.
  const box = await page.locator('#map').boundingBox();
  await page.mouse.click(box.x + 40, box.y + box.height - 40);
  await page.waitForTimeout(1200);
  check((await carStatuses(page)).length === 4, 'demo: clicking the map adds a car');

  // The ambulance icon faces its direction of travel (drawn facing east, mirrored going west).
  const sample = () => page.evaluate(() => {
    const m = [...document.querySelectorAll('.leaflet-marker-icon')].find(e => e.querySelector('svg[aria-label="ambulance"]'));
    const r = m.getBoundingClientRect();
    return { x: r.left, flipped: m.firstChild.style.transform === 'scaleX(-1)' };
  });
  let checked = false;
  for (let i = 0; i < 10 && !checked; i++) {
    const a = await sample();
    await page.waitForTimeout(2000);
    const b = await sample();
    if (Math.abs(b.x - a.x) > 4) {            // clearly moving east or west on screen
      const west = b.x < a.x;
      check(b.flipped === west, `demo: ambulance faces ${west ? 'west' : 'east'} while driving ${west ? 'west' : 'east'}`);
      checked = true;
    }
  }
  if (!checked) console.log('SKIP  demo: ambulance was not moving east or west during the sample');

  check(page.errors.length === 0, `demo: no page errors ${page.errors.join(' | ')}`);

  const phone = await newPage(browser, { viewport: { width: 390, height: 844 } });
  await phone.goto(`${BASE}/demo`);
  await phone.waitForTimeout(1500);
  const width = await phone.evaluate(() => document.documentElement.scrollWidth);
  check(width <= 390, `demo: no sideways scrolling on a phone (page is ${width}px wide)`);
}

async function live(browser) {
  // Ambulance at the start of a road; driver 150 m east along it, inside the alert zone.
  const amb = await newPage(browser, {
    viewport: { width: 390, height: 844 }, permissions: ['geolocation'],
    geolocation: { latitude: 17.4433, longitude: 78.4877 },
  });
  await amb.goto(`${BASE}/ambulance`);
  await amb.fill('#driverName', 'Test Driver');
  await amb.fill('#licenseId', 'DL-TEST');
  await amb.setInputFiles('#photoInput', { name: 'p.png', mimeType: 'image/png', buffer: Buffer.from('x') });

  if (await amb.locator('#operatorKey').count()) {
    await amb.fill('#operatorKey', 'definitely-wrong');
    await amb.click('#verifyBtn');
    await amb.waitForTimeout(500);
    check(/incorrect/i.test(await amb.textContent('#verifyMsg')), 'live: wrong operator code is refused');
    if (!KEY) { check(false, 'live: set OPERATOR_KEY to the server\'s key to continue'); return; }
    await amb.fill('#operatorKey', KEY);
  }
  await amb.click('#verifyBtn');
  await amb.waitForSelector('#missionPanel:not(.hidden)', { timeout: 5000 });
  check(true, 'live: ambulance crew verified');

  await amb.waitForTimeout(800);                 // starting GPS fix
  const box = await amb.locator('#map').boundingBox();
  await amb.mouse.click(box.x + box.width * 0.85, box.y + box.height * 0.5);   // destination to the east
  await amb.click('#startBtn');
  await amb.waitForFunction(() => /ACTIVE/.test(document.getElementById('status').textContent), null, { timeout: 15000 });
  check(true, 'live: mission started');

  const drv = await newPage(browser, {
    viewport: { width: 390, height: 844 }, permissions: ['geolocation'],
    geolocation: { latitude: 17.4433, longitude: 78.4877 },
  });
  await drv.goto(`${BASE}/driver`);
  await drv.waitForFunction(() => document.getElementById('banner').classList.contains('show'), null, { timeout: 10000 });
  check(true, 'live: driver on the route sees the alert banner');
  check(/behind you/.test(await drv.textContent('#info')), 'live: alert says the ambulance is behind the driver');

  await amb.click('#startBtn');                  // stop
  await drv.waitForFunction(() => /No active ambulance/.test(document.getElementById('info').textContent), null, { timeout: 10000 });
  check(!(await drv.locator('#banner.show').count()), 'live: alert clears when the mission stops');

  check(amb.errors.length === 0, `live: no ambulance page errors ${amb.errors.join(' | ')}`);
  check(drv.errors.length === 0, `live: no driver page errors ${drv.errors.join(' | ')}`);
}

(async () => {
  const browser = await chromium.launch();
  try {
    await demo(browser);
    await live(browser);
  } catch (e) {
    check(false, `unexpected error: ${e.message}`);
  } finally {
    await browser.close();
  }
  console.log(failures ? `\n${failures} check(s) failed` : '\nAll checks passed');
  process.exit(failures ? 1 : 0);
})();
