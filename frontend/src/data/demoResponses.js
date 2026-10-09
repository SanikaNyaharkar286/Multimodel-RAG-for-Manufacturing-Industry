// SAMPLE DATA ONLY: used while useDemoData is true.
// Real answers must come from your uploaded manuals via the backend.

export const demoImageResponse = {
  demo: true,
  blocks: [
    {
      type: "text",
      content:
        "The component in the image shows signs of wear that may point to a bearing or spindle alignment issue.\n\nCheck the items below against the equipment manual before running the machine.",
    },
    {
      type: "diagram",
      caption: "Spindle assembly diagram (Figure 4-2)",
    },
    {
      type: "table",
      title: "Component details",
      columns: ["Component", "Observation", "Possible cause"],
      rows: [
        ["Spindle bearing", "Surface discoloration", "Overheating / lubrication loss"],
        ["Spindle shaft", "Visible scoring", "Contamination or misalignment"],
        ["Housing seal", "Not clearly visible", "Needs manual inspection"],
      ],
    },
    {
      type: "steps",
      title: "Troubleshooting steps",
      items: [
        "Switch off the machine and apply lockout/tagout.",
        "Remove the spindle cover and inspect the bearing surface.",
        "Check lubrication level and condition.",
        "Measure shaft runout and compare with the manual tolerance.",
        "Replace worn parts and re-test at low speed.",
      ],
    },
  ],
  sources: [
    { document: "Spindle Maintenance Manual.pdf", page: 42, section: "4.3 Bearing inspection" },
    { document: "Spindle Maintenance Manual.pdf", page: 58, section: "Table 6-1 Troubleshooting" },
  ],
};

export const demoTextResponse = {
  demo: true,
  blocks: [
    {
      type: "text",
      content:
        "Error 1001 usually indicates a communication or sensor fault. Verify the exact meaning in the error code table of your manual.",
    },
    {
      type: "table",
      title: "Error code reference",
      columns: ["Code", "Meaning", "Recommended action"],
      rows: [
        ["1001", "Sensor communication fault", "Check cable and connector"],
        ["1002", "Overtemperature warning", "Check coolant and airflow"],
      ],
    },
    {
      type: "steps",
      title: "Suggested checks",
      items: [
        "Power off the machine safely.",
        "Inspect the sensor cable and connectors.",
        "Restart and check whether the error returns.",
      ],
    },
  ],
  sources: [
    { document: "Controller Error Codes.pdf", page: 12, section: "Error code table" },
  ],
};