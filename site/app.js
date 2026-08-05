const DATA_URL = "data/matches.json";

const state = {
  data: null,
  matches: [],
};

const elements = {
  list: document.querySelector("#matches"),
  summary: document.querySelector("#result-summary"),
  overviewSummary: document.querySelector("#overview-summary"),
  categorySummary: document.querySelector("#category-summary"),
  type: document.querySelector("#type-filter"),
  category: document.querySelector("#category-filter"),
  date: document.querySelector("#date-filter"),
  round: document.querySelector("#round-filter"),
  status: document.querySelector("#status-filter"),
  search: document.querySelector("#search-filter"),
  reset: document.querySelector("#reset-filters"),
  generatedAt: document.querySelector("#generated-at"),
};

const CATEGORY_ORDER = ["MS", "MD", "WS", "WD", "TEAM-M", "TEAM-W"];

const CATEGORY_LABELS = {
  MS: "MS（男子シングルス）",
  MD: "MD（男子ダブルス）",
  WS: "WS（女子シングルス）",
  WD: "WD（女子ダブルス）",
  "TEAM-M": "男子団体",
  "TEAM-W": "女子団体",
};

const STATUS_LABELS = {
  available: "動画リンクあり",
  not_played: "未実施",
  unavailable: "要確認",
};

function option(label, value) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = label;
  return element;
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b), "ja", { numeric: true }));
}

function setOptions(select, values, formatter = (value) => value) {
  for (const value of uniqueSorted(values)) {
    select.append(option(formatter(value), value));
  }
}

function dateLabel(value) {
  if (!value) return "日付未確認";
  const [, month, day] = String(value).split("-");
  return `${Number(month)}月${Number(day)}日`;
}

function roundLabel(value) {
  return value || "回戦未確認";
}

function statusLabel(entry) {
  return STATUS_LABELS[entry.status] || "状態未確認";
}

function categoryKey(entry) {
  if (entry.tournamentType === "team") return entry.category === "男子" ? "TEAM-M" : "TEAM-W";
  return entry.category || "UNKNOWN";
}

function categoryLabel(value) {
  return CATEGORY_LABELS[value] || value || "種目未確認";
}

function typeLabel(entry) {
  return entry.tournamentType === "team" ? "団体" : "個人";
}

function searchableText(entry) {
  return [
    entry.tournamentName,
    entry.category,
    categoryLabel(categoryKey(entry)),
    entry.eventTitle,
    entry.round,
    entry.orderName,
    entry.date,
    entry.status,
    entry.statusReason,
    ...((entry.sides || []).flatMap((side) => [
      side.name,
      side.school,
      ...(side.players || []).flatMap((player) => [player.name, player.belong]),
    ])),
  ].join(" ").toLocaleLowerCase("ja");
}

function filteredMatches() {
  const query = elements.search.value.trim().toLocaleLowerCase("ja");
  return state.matches.filter((entry) => {
    if (elements.type.value !== "all" && entry.tournamentType !== elements.type.value) return false;
    if (elements.category.value !== "all" && categoryKey(entry) !== elements.category.value) return false;
    if (elements.date.value !== "all" && entry.date !== elements.date.value) return false;
    if (elements.round.value !== "all" && (entry.round || "round-unknown") !== elements.round.value) return false;
    if (elements.status.value !== "all" && entry.status !== elements.status.value) return false;
    if (query && !searchableText(entry).includes(query)) return false;
    return true;
  });
}

function filterSummary() {
  const type = elements.type.value === "all" ? "全種別" : elements.type.value === "team" ? "団体" : "個人";
  const category = elements.category.value === "all" ? "全種目" : categoryLabel(elements.category.value);
  const date = elements.date.value === "all" ? "全日付" : dateLabel(elements.date.value);
  const round = elements.round.value === "all" ? "全回戦" : roundLabel(elements.round.value);
  const status = elements.status.value === "all" ? "状態すべて" : statusLabel({ status: elements.status.value });
  return `${type} / ${category} / ${date} / ${round} / ${status}`;
}

function resetFilterControls() {
  elements.type.value = "all";
  elements.category.value = "all";
  elements.date.value = "all";
  elements.round.value = "all";
  elements.status.value = "all";
  elements.search.value = "";
}

function appendText(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = text;
  parent.append(element);
  return element;
}

function renderOverview() {
  const counts = state.data?.counts || {};
  elements.overviewSummary.replaceChildren();
  const total = Number(counts.total) || state.matches.length;
  const available = Number(counts.available) || 0;
  const notPlayed = Number(counts.notPlayed) || 0;
  const unavailable = Number(counts.unavailable) || 0;
  const strong = document.createElement("strong");
  strong.textContent = total.toLocaleString("ja-JP");
  elements.overviewSummary.append(strong, "試合　/　", `動画リンク ${available.toLocaleString("ja-JP")}　/　`, `未実施 ${notPlayed.toLocaleString("ja-JP")}　/　`, `要確認 ${unavailable.toLocaleString("ja-JP")}`);

  const aggregates = new Map();
  for (const entry of state.matches) {
    const key = categoryKey(entry);
    if (!aggregates.has(key)) aggregates.set(key, { total: 0, available: 0, not_played: 0, unavailable: 0 });
    const row = aggregates.get(key);
    row.total += 1;
    if (row[entry.status] !== undefined) row[entry.status] += 1;
  }

  elements.categorySummary.replaceChildren();
  const orderedKeys = [...CATEGORY_ORDER, ...[...aggregates.keys()].filter((key) => !CATEGORY_ORDER.includes(key))];
  for (const key of orderedKeys) {
    const row = aggregates.get(key);
    if (!row) continue;
    const tr = document.createElement("tr");
    appendText(tr, "th", "summary-table__name", categoryLabel(key)).scope = "row";
    appendText(tr, "td", "", row.total.toLocaleString("ja-JP"));
    appendText(tr, "td", "", row.available.toLocaleString("ja-JP"));
    appendText(tr, "td", "", row.not_played.toLocaleString("ja-JP"));
    appendText(tr, "td", "", row.unavailable.toLocaleString("ja-JP"));
    elements.categorySummary.append(tr);
  }
}

function renderSides(parent, entry) {
  const sides = entry.sides || [];
  if (!sides.length) {
    appendText(parent, "p", "side-line side-line--empty", "対戦者名はBIRD SCOREで確認");
    return;
  }
  const winnerIndex = entry.result?.winnerIndex === 0 || entry.result?.winnerIndex === 1
    ? entry.result.winnerIndex
    : null;
  sides.forEach((side, index) => {
    const line = document.createElement("p");
    line.className = "side-line";
    if (winnerIndex === index) line.classList.add("side-line--winner");
    else if (winnerIndex !== null) line.classList.add("side-line--loser");

    appendText(line, "strong", "side-name", side.name || `${index + 1}側`);
    if (side.school && side.school !== side.name) appendText(line, "span", "side-school", side.school);
    if (side.players?.length) {
      const players = side.players.map((player) => player.name).filter(Boolean).join("・");
      if (players && players !== side.name) appendText(line, "span", "side-players", `（${players}）`);
    }
    if (winnerIndex === index) appendText(line, "span", "side-result-badge", "勝者");
    parent.append(line);
  });
}

function resultInfo(entry) {
  const result = entry.result;
  if (!result) return { winnerIndex: null, winnerName: "", reasons: [] };

  const winnerIndex = result.winnerIndex === 0 || result.winnerIndex === 1 ? result.winnerIndex : null;
  const winnerName = result.winnerName || (winnerIndex !== null ? entry.sides?.[winnerIndex]?.name || "" : "");
  const reasons = (result.reasonsForLoss || [])
    .map((reason, index) => reason ? `${entry.sides?.[index]?.name || `${index + 1}側`}: ${reason}` : "")
    .filter(Boolean);
  return { winnerIndex, winnerName, reasons };
}

function renderResult(parent, entry) {
  if (!entry.result) return;
  const { winnerName, reasons } = resultInfo(entry);
  const banner = document.createElement("div");
  banner.className = "result-banner";
  appendText(banner, "span", "result-banner__label", winnerName ? "勝者" : "結果");
  if (winnerName) appendText(banner, "strong", "result-banner__winner", winnerName);
  else if (!winnerName) appendText(banner, "span", "result-banner__status", "確認済み");
  parent.append(banner);
  if (reasons.length) appendText(parent, "p", "result-banner__reason", reasons.join(" / "));
}

function renderScore(parent, entry) {
  const score = entry.score;
  const games = (score?.games || [])
    .filter(Array.isArray)
    .filter((game) => game.some((point) => Number.isFinite(Number(point)) && Number(point) > 0));
  const sides = entry.sides || [];
  if (!games.length || sides.length < 2) return;

  const block = document.createElement("div");
  block.className = "score-board";
  const heading = document.createElement("div");
  heading.className = "score-board__heading";
  appendText(heading, "span", "score-board__title", "スコア");
  const gameWins = (score.gameWins || []).map(Number).filter(Number.isFinite);
  if (gameWins.length === sides.length) {
    const gameTotal = document.createElement("span");
    gameTotal.className = "score-board__game-total";
    appendText(gameTotal, "span", "score-board__game-label", "ゲーム結果");
    appendText(gameTotal, "strong", "score-board__game-value", gameWins.join("–"));
    heading.append(gameTotal);
  }
  block.append(heading);

  const tableWrap = document.createElement("div");
  tableWrap.className = "score-board__table-wrap";
  const table = document.createElement("table");
  table.className = "score-board__table";
  table.setAttribute("aria-label", "試合スコア");
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  appendText(headerRow, "th", "score-board__side-heading", "対戦者");
  games.forEach((_, index) => appendText(headerRow, "th", "score-board__game-heading", `第${index + 1}G`));
  if (gameWins.length === sides.length) appendText(headerRow, "th", "score-board__wins-heading", "勝ゲーム");
  thead.append(headerRow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  const { winnerIndex } = resultInfo(entry);
  sides.slice(0, 2).forEach((side, sideIndex) => {
    const row = document.createElement("tr");
    row.className = "score-board__row";
    if (winnerIndex === sideIndex) row.classList.add("score-board__row--winner");

    const sideCell = document.createElement("th");
    sideCell.scope = "row";
    sideCell.className = "score-board__side";
    appendText(sideCell, "span", "score-board__side-name", side.name || `${sideIndex + 1}側`);
    if (side.school && side.school !== side.name) appendText(sideCell, "span", "score-board__side-school", side.school);
    row.append(sideCell);

    games.forEach((game) => {
      const point = game[sideIndex];
      const cell = document.createElement("td");
      cell.className = "score-board__point";
      cell.textContent = Number.isFinite(Number(point)) ? String(point) : "—";
      row.append(cell);
    });
    if (gameWins.length === sides.length) appendText(row, "td", "score-board__game-wins", String(gameWins[sideIndex]));
    tbody.append(row);
  });
  table.append(tbody);
  tableWrap.append(table);
  block.append(tableWrap);
  parent.append(block);
}

function renderCard(entry) {
  const card = document.createElement("article");
  card.className = "match-card";

  const content = document.createElement("div");
  content.className = "match-card__content";
  renderResult(content, entry);

  const sides = document.createElement("div");
  sides.className = "match-card__sides";
  renderSides(sides, entry);
  if (entry.result || entry.score?.games?.length) {
    const resultBlock = document.createElement("div");
    resultBlock.className = "match-card__result";
    renderScore(resultBlock, entry);
    sides.append(resultBlock);
  }
  content.append(sides);

  const action = document.createElement("div");
  action.className = "match-card__action";
  if (entry.birdScoreUrl) {
    const scoreLink = document.createElement("a");
    scoreLink.className = "score-button";
    scoreLink.href = entry.birdScoreUrl;
    scoreLink.target = "_blank";
    scoreLink.rel = "noreferrer";
    scoreLink.textContent = "BIRD SCORE";
    action.append(scoreLink);
  }
  if (entry.status === "available" && entry.archiveUrl) {
    const link = document.createElement("a");
    link.className = "video-button";
    link.href = entry.archiveUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "動画を見る";
    link.title = `${entry.archiveTitle || "インハイTV公式アーカイブ"} ${entry.startSeconds}秒から`;
    action.append(link);
  } else {
    const unavailable = document.createElement("span");
    unavailable.className = "unavailable-button";
    unavailable.title = entry.statusReason || "動画リンクを確認できません。";
    unavailable.textContent = statusLabel(entry);
    action.append(unavailable);
    if (entry.statusReason) appendText(action, "span", "unavailable-detail", entry.statusReason);
  }

  card.append(content, action);
  return card;
}

function groupKey(entry) {
  return [entry.date, entry.tournamentType, categoryKey(entry), entry.round || "round-unknown"].join("|");
}

function groupMatches(matches) {
  const groups = new Map();
  for (const entry of matches) {
    const key = groupKey(entry);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  return [...groups.values()];
}

function renderGroup(entries) {
  const first = entries[0];
  const group = document.createElement("section");
  group.className = "match-group";

  const heading = document.createElement("div");
  heading.className = "match-group__header";
  appendText(heading, "h3", "match-group__title", `${dateLabel(first.date)} / ${typeLabel(first)} / ${categoryLabel(categoryKey(first))} / ${roundLabel(first.round)}`);
  appendText(heading, "p", "match-group__meta", `${entries.length.toLocaleString("ja-JP")}試合`);

  const list = document.createElement("div");
  list.className = "match-group__list";
  for (const entry of entries) list.append(renderCard(entry));
  group.append(heading, list);
  return group;
}

function render() {
  const matches = filteredMatches();
  elements.list.replaceChildren();
  const groups = groupMatches(matches);
  if (!matches.length) {
    appendText(elements.list, "p", "empty-state", "条件に一致する試合がありません。");
  } else {
    const fragment = document.createDocumentFragment();
    groups.forEach((group) => fragment.append(renderGroup(group)));
    elements.list.append(fragment);
  }
  elements.summary.textContent = `${matches.length.toLocaleString("ja-JP")}件表示 / 全${state.matches.length.toLocaleString("ja-JP")}件 · ${filterSummary()}`;
}

function initialiseFilters() {
  resetFilterControls();
  setOptions(elements.category, state.matches.map(categoryKey), categoryLabel);
  setOptions(elements.date, state.matches.map((entry) => entry.date), dateLabel);
  setOptions(elements.round, state.matches.map((entry) => entry.round || "round-unknown"), roundLabel);
  [elements.type, elements.category, elements.date, elements.round, elements.status, elements.search].forEach((element) => {
    element.addEventListener("input", render);
    element.addEventListener("change", render);
  });
  elements.reset.addEventListener("click", () => {
    resetFilterControls();
    render();
  });
}

async function load() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`データ取得に失敗しました（${response.status}）`);
    state.data = await response.json();
    state.matches = Array.isArray(state.data.matches) ? state.data.matches : [];
    if (state.data.generatedAt) elements.generatedAt.textContent = `更新: ${new Date(state.data.generatedAt).toLocaleString("ja-JP")}`;
    renderOverview();
    initialiseFilters();
    render();
  } catch (error) {
    elements.list.replaceChildren();
    appendText(elements.list, "p", "error-state", `データを読み込めませんでした。${error.message}`);
    elements.summary.textContent = "読み込みエラー";
    elements.overviewSummary.textContent = "データを読み込めませんでした。";
  }
}

load();
