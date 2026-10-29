"use strict";

const previews = {
  select: {
    src: "./assets/champion-select.webp",
    alt: "Mayhem Companion showing available champion win rates and summoner spell-pair statistics.",
  },
  builds: {
    src: "./assets/item-builds.webp",
    alt: "Mayhem Companion showing item-build choices with item icons and sample statistics.",
  },
};
const tabs = [...document.querySelectorAll("[data-preview]")];
const screenshot = document.getElementById("app-screenshot");
const panel = document.getElementById("preview-panel");

function selectPreview(tab) {
  const selected = previews[tab.dataset.preview];
  if (!selected) return;
  for (const candidate of tabs) {
    const active = candidate === tab;
    candidate.setAttribute("aria-selected", String(active));
    candidate.tabIndex = active ? 0 : -1;
  }
  screenshot.src = selected.src;
  screenshot.alt = selected.alt;
  panel.setAttribute("aria-labelledby", tab.id);
}

for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectPreview(tab));
  tab.addEventListener("keydown", (event) => {
    let target;
    if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
    else if (event.key === "ArrowLeft") target = tabs[(index + tabs.length - 1) % tabs.length];
    else if (event.key === "Home") target = tabs[0];
    else if (event.key === "End") target = tabs[tabs.length - 1];
    if (!target) return;
    event.preventDefault();
    selectPreview(target);
    target.focus();
  });
}

function revealLinkedDetails() {
  if (location.hash !== "#limitations" && location.hash !== "#privacy") return;
  const target = document.getElementById(location.hash.slice(1));
  if (target instanceof HTMLDetailsElement) {
    target.open = true;
    target.scrollIntoView({ block: "start" });
  }
}
window.addEventListener("hashchange", revealLinkedDetails);
revealLinkedDetails();
