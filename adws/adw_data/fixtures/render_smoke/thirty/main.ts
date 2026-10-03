document.getElementById("t")!.textContent = "A page with enough text to count as drawn. ".repeat(10);
const b = document.getElementById("b")!;
for (let i = 1; i <= 30; i++) {
  const el = document.createElement("button");
  el.textContent = `Button ${i}`;
  el.onclick = () => { if (i > 25) throw new Error(`button ${i} is broken`); };
  b.appendChild(el);
}
