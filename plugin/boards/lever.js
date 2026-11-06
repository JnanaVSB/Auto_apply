const BOARD = {
  anchor: "#application-form input[name=email]",
  question: ".application-question",
  label: ".application-label",
  parse: "/parseResume",
  kindOf(key) {
    if (key === "location") return "location";
    return key.startsWith("consent[") ? "consent" : undefined;
  },
};
