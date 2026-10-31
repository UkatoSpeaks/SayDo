import { render, screen } from "@testing-library/react";
import { LedgerCellView } from "./LedgerCellView";

function renderCell(kind: Parameters<typeof LedgerCellView>[0]["cell"]["kind"], text = "") {
  render(
    <table>
      <tbody>
        <tr>
          <LedgerCellView cell={{ period: "Q3FY26", kind, text }} />
        </tr>
      </tbody>
    </table>,
  );
  return screen.getByRole("cell");
}

describe("LedgerCellView", () => {
  it("shows the glyph and text, with a spoken label", () => {
    const cell = renderCell("lowered", "400");
    expect(cell).toHaveTextContent("▼400");
    expect(cell).toHaveTextContent("Q3FY26: lowered 400");
    expect(cell.className).toContain("text-missed");
    expect(cell.className).not.toContain("tint");
  });

  it("tints kept, missed, moved and open cells", () => {
    expect(renderCell("kept", "410").className).toContain("bg-kept-tint");
  });

  it.each([
    ["missed", "bg-missed-tint"],
    ["moved", "bg-moved-tint"],
    ["open", "bg-open-tint"],
  ] as const)("%s cells get %s", (kind, tint) => {
    expect(renderCell(kind, "x").className).toContain(tint);
  });

  it("does not repeat the arrow when the text already has it", () => {
    const cell = renderCell("moved", "→ Q3FY26");
    expect(cell.querySelector("[aria-hidden]")?.textContent).toBe("→ Q3FY26");
    expect(cell).toHaveTextContent("Q3FY26: deadline moved → Q3FY26");
  });

  it("marks silence with a dot and says 'not mentioned'", () => {
    const cell = renderCell("silent");
    expect(cell).toHaveTextContent("·");
    expect(cell).toHaveTextContent("Q3FY26: not mentioned");
  });

  it("renders an empty cell for none, with screen-reader text only", () => {
    const cell = renderCell("none");
    expect(cell.querySelector("[aria-hidden]")).toBeNull();
    expect(cell).toHaveTextContent("Q3FY26: no entry");
  });
});
