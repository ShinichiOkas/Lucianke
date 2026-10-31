interface Cfg {
  db: { host: string; port: number };
}

export class Server {
  opts: { z: number } = { z: 1 };

  run(c: Cfg, h: Cfg['db']): string {
    return c.db.host + h.port + this.opts.z;
  }
}
