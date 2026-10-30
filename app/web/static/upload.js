// Uploads the chosen .tex files without a file-upload library: their text is read here in the browser and sent with
// the form as file.N.name and file.N.latex (the file box itself has no name, so it is never sent).
for (const form of document.querySelectorAll("form[data-upload]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = [...form.querySelector("input[type=file]").files];
    for (const [number, file] of files.entries()) {
      for (const [name, value] of [["name", file.name], ["latex", await file.text()]]) {
        const field = document.createElement("input");
        Object.assign(field, { type: "hidden", name: `file.${number}.${name}`, value });
        form.append(field);
      }
    }
    form.submit();
  });
}
