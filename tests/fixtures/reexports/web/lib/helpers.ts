export function flush(): number {
  return 0;
}

function internal(): number {
  return 1;
}

export default function main(): number {
  return internal();
}
