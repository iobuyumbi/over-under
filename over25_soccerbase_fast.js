#!/usr/bin/env node

const TARGET_DATE = process.argv.find((arg) => /^\d{4}-\d{2}-\d{2}$/.test(arg)) ||
  new Date().toISOString().slice(0, 10);
const INCLUDE_COMPLETED = process.argv.includes("--all");
const PRETTY = process.argv.includes("--pretty");
const SAVE = !process.argv.includes("--no-save");
const CONCURRENCY = Number(process.env.OVER25_CONCURRENCY || 8);

const HEADERS = {
  "user-agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
};

function clean(value = "") {
  return value
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&#039;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/<[^>]+>/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

async function fetchText(url, retries = 3) {
  let lastError;
  for (let i = 0; i < retries; i += 1) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 25_000);
      const response = await fetch(url, { headers: HEADERS, signal: controller.signal });
      clearTimeout(timeout);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.text();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 700 * (i + 1)));
    }
  }
  throw lastError;
}

async function mapLimit(items, limit, mapper) {
  const results = new Array(items.length);
  let index = 0;
  async function worker() {
    while (index < items.length) {
      const current = index;
      index += 1;
      results[current] = await mapper(items[current], current);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

function extractTeam(row, className) {
  const pattern = new RegExp(
    `<td class="team ${className}[^"]*">[\\s\\S]*?<a href="[^"]*team_id=(\\d+)[^"]*"[^>]*>([\\s\\S]*?)<\\/a>`,
  );
  const match = row.match(pattern);
  return match ? { id: match[1], name: clean(match[2]) } : null;
}

function extractScore(row) {
  const match = row.match(
    /<td class="score">[\s\S]*?<em>(\d+)<\/em>[\s\S]*?<em>(\d+)<\/em>[\s\S]*?<\/td>/,
  );
  return match ? { home: Number(match[1]), away: Number(match[2]) } : null;
}

function extractDateTime(row) {
  const hidden = row.match(/<span class="hide">(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})<\/span>/);
  if (hidden) return `${hidden[1]} ${hidden[2]}`;
  const linked = row.match(/date=(\d{4}-\d{2}-\d{2})/);
  return linked ? `${linked[1]} 00:00` : null;
}

function extractCompetition(row) {
  const match = row.match(/<td class="first tournament">[\s\S]*?<a [^>]*>([\s\S]*?)<\/a>/);
  return match ? clean(match[1]) : "";
}

function matchRows(html) {
  return [...html.matchAll(/<tr class="match"[\s\S]*?<\/tr>/g)].map((match) => match[0]);
}

function parseFixtures(html) {
  let league = "";
  const fixtures = [];
  for (const part of html.split(/<tr/).slice(1)) {
    const row = `<tr${part.split("</tr>")[0]}</tr>`;
    const heading = row.match(/<h2><a [^>]*>([\s\S]*?)<\/a><\/h2>/);
    if (heading) {
      league = clean(heading[1]);
      continue;
    }
    if (!row.includes('class="match"')) continue;
    const home = extractTeam(row, "homeTeam");
    const away = extractTeam(row, "awayTeam");
    if (!home || !away) continue;
    fixtures.push({
      league,
      home,
      away,
      score: extractScore(row),
      neutral: row.includes("neutralVenues"),
    });
  }
  return fixtures;
}

function parseResults(html) {
  return matchRows(html)
    .map((row) => ({
      date: extractDateTime(row),
      league: extractCompetition(row),
      home: extractTeam(row, "homeTeam"),
      away: extractTeam(row, "awayTeam"),
      score: extractScore(row),
    }))
    .filter((match) => match.date && match.home && match.away && match.score)
    .sort((a, b) => a.date.localeCompare(b.date));
}

function totalGoals(match) {
  return match.score.home + match.score.away;
}

function formatMatch(match) {
  return `${match.date.slice(0, 10)} ${match.home.name} ${match.score.home}-${match.score.away} ${match.away.name}`;
}

async function run(date) {
  const fixturePage = await fetchText(`https://www.soccerbase.com/matches/results.sd?date=${date}`);
  let fixtures = parseFixtures(fixturePage);
  if (!INCLUDE_COMPLETED) fixtures = fixtures.filter((fixture) => !fixture.score);

  const teamIds = [...new Set(fixtures.flatMap((fixture) => [fixture.home.id, fixture.away.id]))];
  const fetched = await mapLimit(teamIds, CONCURRENCY, async (id) => {
    const html = await fetchText(`https://www.soccerbase.com/teams/team.sd?team_id=${id}&teamTabs=results`);
    return [id, parseResults(html)];
  });
  const resultMap = Object.fromEntries(fetched);
  const cutoff = `${date} 00:00`;

  const evaluated = fixtures.map((fixture) => {
    const homeResults = (resultMap[fixture.home.id] || []).filter((match) => match.date < cutoff);
    const awayResults = (resultMap[fixture.away.id] || []).filter((match) => match.date < cutoff);
    const homeLast3 = homeResults.filter((match) => match.home.id === fixture.home.id).slice(-3);
    const awayLast3 = awayResults.filter((match) => match.away.id === fixture.away.id).slice(-3);
    const checks = {
      H1: homeLast3.length === 3 && homeLast3.reduce((sum, match) => sum + totalGoals(match), 0) >= 7,
      H2: homeLast3.length === 3 && homeLast3.filter((match) => totalGoals(match) > 2.5).length >= 2,
      A1: awayLast3.length === 3 && awayLast3.reduce((sum, match) => sum + totalGoals(match), 0) >= 7,
      A2: awayLast3.length >= 1 && totalGoals(awayLast3[awayLast3.length - 1]) >= 2,
      A3: awayLast3.length === 3 && awayLast3.filter((match) => match.score.away > 0).length >= 2,
      A4: awayLast3.length === 3 && awayLast3.filter((match) => totalGoals(match) > 2.5).length >= 2,
    };
    const passCount = Object.values(checks).filter(Boolean).length;
    return {
      league: fixture.league,
      match: `${fixture.home.name} vs ${fixture.away.name}`,
      score: fixture.score ? `${fixture.score.home}-${fixture.score.away}` : null,
      pass: passCount === 6,
      passCount,
      checks,
      homeLast3: homeLast3.map(formatMatch),
      awayLast3: awayLast3.map(formatMatch),
    };
  });

  return {
    date,
    onlyScheduled: !INCLUDE_COMPLETED,
    fixtureCount: fixtures.length,
    teamCount: teamIds.length,
    pass: evaluated.filter((item) => item.pass),
    near: evaluated.filter((item) => !item.pass && item.passCount >= 5),
  };
}

run(TARGET_DATE)
  .then(async (result) => {
    if (PRETTY) {
      printPretty(result);
    } else {
      console.log(JSON.stringify(result, null, 2));
    }
    if (SAVE) {
      const fs = await import("node:fs/promises");
      const outputPath = `predictions_soccerbase_${TARGET_DATE}.json`;
      await fs.writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
      if (PRETTY) console.log(`\nResults saved to: ${outputPath}`);
    }
  })
  .catch((error) => {
    console.error(`[ERROR] ${error.message}`);
    process.exit(1);
  });

function checkMarks(checks) {
  return Object.entries(checks)
    .map(([key, value]) => `${key}: ${value ? "PASS" : "FAIL"}`)
    .join(", ");
}

function printPretty(result) {
  console.log("=".repeat(70));
  console.log("OVER 2.5 GOALS PREDICTOR - SOCCERBASE FAST");
  console.log(`Date: ${result.date}`);
  console.log(`Fixtures analyzed: ${result.fixtureCount}`);
  console.log(`Teams: ${result.teamCount}`);
  console.log("=".repeat(70));

  console.log("\nQUALIFIED MATCHES (6/6 checks):");
  if (!result.pass.length) {
    console.log("\nNone");
  }
  for (const item of result.pass) {
    console.log(`\n- ${item.league}: ${item.match}`);
    if (item.score) console.log(`  Score: ${item.score}`);
    console.log(`  Home last 3: ${item.homeLast3.join(", ")}`);
    console.log(`  Away last 3: ${item.awayLast3.join(", ")}`);
    console.log(`  Checks: ${checkMarks(item.checks)}`);
  }

  console.log("\nCLOSE CALLS (5/6 checks):");
  if (!result.near.length) {
    console.log("\nNone");
  }
  for (const item of result.near) {
    const failed = Object.entries(item.checks)
      .filter(([, value]) => !value)
      .map(([key]) => key)
      .join(", ");
    console.log(`\n- ${item.league}: ${item.match}`);
    if (item.score) console.log(`  Score: ${item.score}`);
    console.log(`  Failed: ${failed}`);
    console.log(`  Home last 3: ${item.homeLast3.join(", ")}`);
    console.log(`  Away last 3: ${item.awayLast3.join(", ")}`);
    console.log(`  Checks: ${checkMarks(item.checks)}`);
  }
  console.log("\n" + "=".repeat(70));
}
