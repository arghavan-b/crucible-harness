"""Build a real PDF fixture so the pdfplumber path is actually exercised.

Why this exists: the plain-text fixture tests extraction *given* clean text, but
skips everything that makes real papers hard — PDF text extraction, and finding
a results table as a structured grid rather than as flat prose. `parse_pdf`
pulls tables via pdfplumber and figures via PyMuPDF; neither was covered.

The content is the real CTGCN paper excerpt (arXiv:2003.09902) and the table is
drawn as a ruled grid, which is what pdfplumber's table detection keys off.

Honest limits: this is single-column, and arXiv's actual PDF is two-column with
ligatures and hyphenation. It tests that the parsing path works, not that it
survives real typesetting. Drop the genuine PDF at
tests/fixtures/ctgcn_paper.pdf and the same tests will run against it.

Regenerate with:  python tests/fixtures/make_pdf.py
Requires reportlab (dev-only; not a package dependency).
"""

from __future__ import annotations

import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ctgcn_paper_generated.pdf")

ABSTRACT = (
    "Abstract—Graph representation learning is a fundamental task in various "
    "applications that strives to learn low-dimensional embeddings for nodes. "
    "We propose a novel k-core based temporal graph convolutional network, the "
    "CTGCN, to learn node representations for dynamic graphs. Experimental "
    "results on 7 real-world graphs demonstrate that the CTGCN outperforms "
    "existing state-of-the-art graph embedding methods in several tasks, "
    "including link prediction and structural role classification."
)

SETUP = (
    "5.2 Link Prediction. The area under the curve (AUC) is utilized as the "
    "evaluation metric, and the averaged AUC scores are reported as link "
    "prediction results. We compute edge feature vectors by utilizing the "
    "Hadamard operation between embedding vectors of node pairs. We train a "
    "logistic regression (LR) classifier to discriminate positive and negative "
    "edge samples. In practice, we split the datasets by month and remove "
    "incomplete data. The dimensionality of embeddings d is set to 128 for all "
    "compared methods."
)

DISCUSSION = (
    "We report link prediction results on these real-world graphs where the "
    "best results are indicated in bold, as illustrated in Table 4. It can be "
    "observed that the proposed CTGCN-C method significantly outperforms other "
    "compared methods across all dynamic graphs, achieving an average AUC of "
    "0.9434 on UCI compared to 0.9375 for the strongest baseline."
)

TABLE_4 = [
    ["Method", "UCI", "AS", "Math", "Facebook", "Enron"],
    ["GCN", "0.7729", "0.7835", "0.7986", "0.6942", "0.7879"],
    ["GAT", "0.7668", "0.7906", "0.8351", "0.6991", "0.8344"],
    ["GIN", "0.8366", "0.8571", "0.8681", "0.7415", "0.8453"],
    ["CGCN-C", "0.9287", "0.9330", "0.9145", "0.8025", "0.9211"],
    ["CGCN-S", "0.9375", "0.9317", "0.9119", "0.8257", "0.9076"],
    ["DynGEM", "0.9032", "0.9372", "0.9025", "0.8019", "0.8926"],
    ["GCRN", "0.8579", "0.8648", "0.8217", "0.7262", "0.8807"],
    ["EvolveGCN", "0.9102", "0.9227", "0.9034", "0.8056", "0.9025"],
    ["CTGCN-C", "0.9434", "0.9578", "0.9691", "0.8836", "0.9769"],
    ["CTGCN-S", "0.9403", "0.9630", "0.9266", "0.8324", "0.9321"],
]


def build(path: str = OUT) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=letter, title="CTGCN")
    story = [
        Paragraph("K-Core based Temporal Graph Convolutional Network for Dynamic Graphs",
                  styles["Title"]),
        Paragraph(ABSTRACT, styles["BodyText"]),
        Spacer(1, 12),
        Paragraph(SETUP, styles["BodyText"]),
        Spacer(1, 12),
        Paragraph("TABLE 4: Average AUC scores of all timestamps for link prediction.",
                  styles["Heading4"]),
        Table(
            TABLE_4,
            style=TableStyle([
                # Ruled grid: pdfplumber's table detection keys off the lines.
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]),
        ),
        Spacer(1, 12),
        Paragraph(DISCUSSION, styles["BodyText"]),
    ]
    doc.build(story)
    return path


if __name__ == "__main__":
    print(f"wrote {build()}")
