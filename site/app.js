const DATA_URL = "data/matches.json";

const state = {
  data: null,
  matches: [],
};

const elements = {
  list: document.querySelector("#matches"),
  summary: document.querySelector("#result-summary"),
  type: document.querySelector("#type-filter"),
  category: document.querySelector("#category-filter"),
  date: document.querySelector("#date-filter"),
  court: document.querySelector("#court-filter"),
  search: document.querySelector("#search-filter"),
  availableOnly: document.querySelector("#available-only"),
  reset: document.querySelector("#reset-filters"),
  generatedAt: document.querySelector("#generated-at"),
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
  return `${month}/${day}`;
}

function statusLabel(entry) {
  if (entry.status === "not_played") return "未実施";
  if (entry.status === "unavailable") return "動画未確認";
  return "動画リンクあり";
}

const CATEGORY_LABELS = {
  MS: "MS（男子シングルス）",
  WS: "WS（女子シングルス）",
  MD: "MD（男子ダブルス）",
  WD: "WD（女子ダブルス）",
  男子: "男子団体",
  女子: "女子団体",
};

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
    entry.eventTitle,
    entry.round,
    entry.matchNo,
    entry.orderName,
    entry.date,
    entry.court,
    ...((entry.sides || []).flatMap((side) => [side.name, ...(side.players || []).flatMap((player) => [player.name, player.belong])])),
  ].join(" ").toLocaleLowerCase("ja");
}

function filteredMatches() {
  const query = elements.search.value.trim().toLocaleLowerCase("ja");
  return state.matches.filter((entry) => {
    if (elements.type.value !== "all" && entry.tournamentType !== elements.type.value) return false;
    if (elements.category.value !== "all" && entry.category !== elements.category.value) return false;
    if (elements.date.value !== "all" && entry.date !== elements.date.value) return false;
    if (elements.court.value !== "all" && entry.court !== elements.court.value) return false;
    if (elements.availableOnly.checked && entry.status !== "available") return false;
    if (query && !searchableText(entry).includes(query)) return false;
    return true;
  });
}

function filterSummary() {
  const type = elements.type.value === "all" ? "全種別" : elements.type.value === "team" ? "団体" : "個人";
  const category = elements.category.value === "all" ? "全種目" : categoryLabel(elements.category.value);
  const date = elements.date.value === "all" ? "全日付" : dateLabel(elements.date.value);
  const court = elements.court.value === "all" ? "全コート" : `${elements.court.value}コート`;
  const availability = elements.availableOnly.checked ? "動画リンクありのみ" : "状態すべて";
  return `${type} / ${category} / ${date} / ${court} / ${availability}`;
}

function resetFilterControls() {
  elements.type.value = "all";
  elements.category.value = "all";
  elements.date.value = "all";
  elements.court.value = "all";
  elements.search.value = "";
  elements.availableOnly.checked = false;
}

function appendText(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = text;
  parent.append(element);
  return element;
}

function groupKey(entry) {
  return [entry.date, entry.tournamentType, entry.category, entry.round || "round-unknown"].join("|");
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

    appendText(line, "strong", "side-name", side.name || `${index + 1}チーム`);
    if (side.school) {
      const school = document.createElement("span");
      school.className = "side-school";
      school.textContent = `学校: ${side.school}`;
      line.append(" ", school);
    }
    if (side.players?.length) {
      const players = side.players.map((player) => player.name).filter(Boolean).join("・");
      if (players && players !== side.name) {
        const detail = document.createElement("span");
        detail.className = "side-players";
        detail.textContent = `（${players}）`;
        line.append(" ", detail);
      }
    }
    if (winnerIndex === index) appendText(line, "span", "side-result-badge", "勝者");
    parent.append(line);
  });
}

function resultDetail(entry) {
  const result = entry.result;
  if (!result) return "";
  const reasons = (result.reasonsForLoss || [])
    .map((reason, index) => reason ? `${entry.sides?.[index]?.name || `${index + 1}側`}: ${reason}` : "")
    .filter(Boolean);
  if (reasons.length) return reasons.join(" / ");
  return result.winnerIndex === null ? "結果確認済み" : "";
}

function renderScore(parent, entry) {
  const score = entry.score;
  if (!score?.games?.length || !entry.sides?.length) return;
  const games = score.games
    .filter((game) => game.some((point) => Number.isFinite(point)))
  if (!games.length) return;

  const block = document.createElement("div");
  block.className = "score-block";
  const label = document.createElement("p");
  label.className = "score-block__label";
  label.append("ゲーム別スコア");
  const gameWins = (score.gameWins || []).filter((wins) => Number.isFinite(wins));
  if (gameWins.length === 2) {
    appendText(label, "span", "score-block__summary", `ゲーム ${gameWins[0]}-${gameWins[1]}`);
  }
  block.append(label);

  const tableWrap = document.createElement("div");
  tableWrap.className = "score-table-wrap";
  const table = document.createElement("table");
  table.className = "score-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  appendText(headRow, "th", "score-table__side", "対戦者");
  games.forEach((_, index) => appendText(headRow, "th", "score-table__game", `第${index + 1}G`));
  appendText(headRow, "th", "score-table__wins", "勝ゲーム");
  head.append(headRow);
  table.append(head);

  const body = document.createElement("tbody");
  const winnerIndex = entry.result?.winnerIndex === 0 || entry.result?.winnerIndex === 1
    ? entry.result.winnerIndex
    : null;
  entry.sides.slice(0, 2).forEach((side, sideIndex) => {
    const row = document.createElement("tr");
    if (winnerIndex === sideIndex) row.className = "score-table__winner";
    else if (winnerIndex !== null) row.className = "score-table__loser";
    const name = side.name || `${sideIndex + 1}側`;
    appendText(row, "th", "score-table__side", name).scope = "row";
    games.forEach((game) => {
      const point = Number.isFinite(game[sideIndex]) ? game[sideIndex] : "—";
      appendText(row, "td", "score-table__game", String(point));
    });
    const wins = Number.isFinite(score.gameWins?.[sideIndex]) ? score.gameWins[sideIndex] : "—";
    appendText(row, "td", "score-table__wins", String(wins));
    body.append(row);
  });
  table.append(body);
  tableWrap.append(table);
  block.append(tableWrap);
  parent.append(block);
}

function renderCard(entry) {
  const card = document.createElement("article");
  card.className = "match-card";

  const meta = document.createElement("div");
  meta.className = "match-card__meta";
  appendText(meta, "span", "match-card__eyebrow", `${typeLabel(entry)} · ${entry.category || "種目未確認"}`);
  appendText(meta, "span", "match-card__detail", `${dateLabel(entry.date)}${entry.court ? ` · ${entry.court}コート` : ""}`);

  const sides = document.createElement("div");
  sides.className = "match-card__sides";
  appendText(sides, "span", "match-card__detail", entry.eventTitle || entry.tournamentName || "");
  renderSides(sides, entry);
  const detail = resultDetail(entry);
  if (detail || entry.score?.games?.length) {
    const resultBlock = document.createElement("div");
    resultBlock.className = "match-card__result";
    if (detail) appendText(resultBlock, "p", "result-line", detail);
    renderScore(resultBlock, entry);
    sides.append(resultBlock);
  }

  const action = document.createElement("div");
  action.className = "match-card__action";
  if (entry.birdScoreUrl) {
    const scoreLink = document.createElement("a");
    scoreLink.className = "score-button";
    scoreLink.href = entry.birdScoreUrl;
    scoreLink.target = "_blank";
    scoreLink.rel = "noreferrer";
    scoreLink.textContent = "BIRD SCORE";
    scoreLink.title = "公式BIRD SCOREを開く";
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
    unavailable.title = entry.statusReason || "開始位置を確認できないため、リンクを有効化していません。";
    unavailable.textContent = statusLabel(entry);
    action.append(unavailable);
  }

  card.append(meta, sides, action);
  return card;
}

function renderGroup(entries) {
  const first = entries[0];
  const group = document.createElement("section");
  group.className = "match-group";

  const heading = document.createElement("div");
  heading.className = "match-group__header";
  const headingText = document.createElement("div");
  headingText.className = "match-group__heading-text";
  appendText(
    headingText,
    "h3",
    "match-group__title",
    `${dateLabel(first.date)} · ${typeLabel(first)} · ${first.eventTitle || first.category || "種目未確認"} · ${first.round || "回戦未確認"}`,
  );
  appendText(
    headingText,
    "p",
    "match-group__meta",
    `${entries.length.toLocaleString("ja-JP")}試合`,
  );
  heading.append(headingText);

  const list = document.createElement("div");
  list.className = "match-group__list";
  for (const entry of entries) list.append(renderCard(entry));
  group.append(heading, list);
  return group;
}

function render() {
  const matches = filteredMatches();
  elements.list.replaceChildren();
  if (!matches.length) {
    appendText(elements.list, "p", "empty-state", "条件に一致する試合がありません。");
  } else {
    const fragment = document.createDocumentFragment();
    const groups = groupMatches(matches);
    groups.forEach((group) => fragment.append(renderGroup(group)));
    elements.list.append(fragment);
    elements.summary.textContent = `${matches.length.toLocaleString("ja-JP")}件表示 / ${groups.length.toLocaleString("ja-JP")}グループ / 全データ${state.matches.length.toLocaleString("ja-JP")}件 · 条件: ${filterSummary()}`;
    return;
  }
  elements.summary.textContent = `${matches.length.toLocaleString("ja-JP")}件表示 / 全データ${state.matches.length.toLocaleString("ja-JP")}件 · 条件: ${filterSummary()}`;
}

function initialiseFilters() {
  // 新しく開いたときは全試合を表示し、前回のブラウザ復元値で件数が減らないようにします。
  resetFilterControls();
  setOptions(elements.category, state.matches.map((entry) => entry.category), categoryLabel);
  setOptions(elements.date, state.matches.map((entry) => entry.date), dateLabel);
  setOptions(elements.court, state.matches.map((entry) => entry.court), (value) => `${value}コート`);
  [elements.type, elements.category, elements.date, elements.court, elements.search, elements.availableOnly].forEach((element) => {
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
    if (state.data.generatedAt) {
      elements.generatedAt.textContent = `データ生成日時: ${new Date(state.data.generatedAt).toLocaleString("ja-JP")}`;
    }
    initialiseFilters();
    render();
  } catch (error) {
    elements.list.replaceChildren();
    appendText(elements.list, "p", "error-state", `データを読み込めませんでした。${error.message}`);
    elements.summary.textContent = "読み込みエラー";
  }
}

load();
