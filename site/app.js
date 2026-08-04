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
  total: document.querySelector("#count-total"),
  available: document.querySelector("#count-available"),
  notPlayed: document.querySelector("#count-not-played"),
  unavailable: document.querySelector("#count-unavailable"),
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

function startTimeLabel(value) {
  if (!value) return "開始時刻未確認";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "開始時刻未確認";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
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

function updateVisibleStats(matches) {
  elements.total.textContent = matches.length.toLocaleString("ja-JP");
  elements.available.textContent = matches.filter((entry) => entry.status === "available").length.toLocaleString("ja-JP");
  elements.notPlayed.textContent = matches.filter((entry) => entry.status === "not_played").length.toLocaleString("ja-JP");
  elements.unavailable.textContent = matches.filter((entry) => entry.status === "unavailable").length.toLocaleString("ja-JP");
}

function appendText(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = text;
  parent.append(element);
  return element;
}

function groupKey(entry) {
  return [entry.date, entry.tournamentType, entry.category, entry.court || "unknown"].join("|");
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
  sides.forEach((side, index) => {
    const line = document.createElement("p");
    line.className = "side-line";
    line.textContent = side.name || `${index + 1}チーム`;
    if (side.players?.length) {
      const players = side.players.map((player) => player.name).filter(Boolean).join("・");
      if (players && players !== side.name) {
        const detail = document.createElement("span");
        detail.textContent = `（${players}）`;
        line.append(" ", detail);
      }
    }
    parent.append(line);
  });
}

function renderCard(entry) {
  const card = document.createElement("article");
  card.className = "match-card";

  const meta = document.createElement("div");
  meta.className = "match-card__meta";
  appendText(meta, "span", "match-card__eyebrow", `${typeLabel(entry)} · ${entry.category || "種目未確認"}`);
  appendText(meta, "strong", "match-card__number", `${entry.matchNo || "試合番号未確認"}${entry.orderName ? ` · ${entry.orderName}` : ""}`);
  appendText(meta, "span", "match-card__detail", `${startTimeLabel(entry.startTime)} · ${entry.round || "ラウンド未確認"}`);

  const sides = document.createElement("div");
  sides.className = "match-card__sides";
  appendText(sides, "span", "match-card__detail", entry.eventTitle || entry.tournamentName || "");
  renderSides(sides, entry);

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
    `${dateLabel(first.date)} · ${typeLabel(first)} · ${first.eventTitle || first.category || "種目未確認"} · ${first.court ? `${first.court}コート` : "コート未確認"}`,
  );
  const availableCount = entries.filter((entry) => entry.status === "available").length;
  appendText(
    headingText,
    "p",
    "match-group__meta",
    `${entries.length.toLocaleString("ja-JP")}試合 · 動画リンク ${availableCount.toLocaleString("ja-JP")}件 · ${first.archiveTitle || "公式アーカイブ未確認"}`,
  );
  heading.append(headingText);

  const archive = entries.find((entry) => entry.archiveUrl)?.archiveUrl;
  if (archive) {
    const archiveLink = document.createElement("a");
    archiveLink.className = "match-group__archive";
    archiveLink.href = archive.split("?")[0];
    archiveLink.target = "_blank";
    archiveLink.rel = "noreferrer";
    archiveLink.textContent = "コートのアーカイブ";
    heading.append(archiveLink);
  }

  const list = document.createElement("div");
  list.className = "match-group__list";
  for (const entry of entries) list.append(renderCard(entry));
  group.append(heading, list);
  return group;
}

function render() {
  const matches = filteredMatches();
  updateVisibleStats(matches);
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
  setOptions(elements.category, state.matches.map((entry) => entry.category), categoryLabel);
  setOptions(elements.date, state.matches.map((entry) => entry.date), dateLabel);
  setOptions(elements.court, state.matches.map((entry) => entry.court), (value) => `${value}コート`);
  [elements.type, elements.category, elements.date, elements.court, elements.search, elements.availableOnly].forEach((element) => {
    element.addEventListener("input", render);
    element.addEventListener("change", render);
  });
  elements.reset.addEventListener("click", () => {
    elements.type.value = "all";
    elements.category.value = "all";
    elements.date.value = "all";
    elements.court.value = "all";
    elements.search.value = "";
    elements.availableOnly.checked = false;
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
