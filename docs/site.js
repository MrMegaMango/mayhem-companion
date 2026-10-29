"use strict";

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
