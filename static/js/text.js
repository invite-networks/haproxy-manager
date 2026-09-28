/* The interface's text. English only: t() fills {name} placeholders and
   tn() picks the singular or plural form by count.

   Values are put in as given: where the result is markup, escape them first. */

function fill(s, vars){
  return String(s).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}
/* The string with its {name} placeholders filled in. */
export function t(s, vars){
  return vars ? fill(s, vars) : s;
}
/* One of two forms by count, {n} filled in: tn(3, "{n} node", "{n} nodes"). */
export function tn(n, one, other, vars){
  return t(Number(n) === 1 ? one : other, Object.assign({ n: n }, vars || {}));
}
