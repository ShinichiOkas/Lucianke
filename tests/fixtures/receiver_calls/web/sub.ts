import { Base } from "./base";

export class Sub extends Base {
  save(): number {
    const later = () => super.save();
    return super.save() + later();
  }
}
