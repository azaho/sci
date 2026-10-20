"""Create three local demonstration publications from reproducible synthetic data.

No cloud writes. Upload only when --push is supplied with an explicit instance.
The database administrator must first enable the printed public key.
"""

import argparse
import json
import math
import random
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sci.client import push
from sci.keys import keygen, load_private, public_jwk
from sci.publication import sign_publication


def create(root):
    root.mkdir(parents=True, exist_ok=True)
    private = root / "demo-author.pem"
    if not private.exists():
        keygen(private)
    rng = random.Random(4816)
    sizes = [1, 2, 4, 8, 16, 32]
    errors = []
    for n in sizes:
        trials = [statistics.mean(rng.gauss(0, 1) for _ in range(n)) for _ in range(512)]
        errors.append(math.sqrt(statistics.mean(v*v for v in trials)))
    csv = "samples,rmse\n" + "".join(f"{n},{e:.6f}\n" for n, e in zip(sizes, errors))
    titles = [
        "Repeated measurements reduce error in a synthetic sensor test",
        "A fixed seed makes the sensor benchmark reproducible",
        "Median estimates resist isolated spikes in a synthetic signal",
    ]
    abstracts = [
        f"In 512 simulated trials at each sample size, averaging 32 independent readings reduced "
        f"root mean square error from {errors[0]:.2f} to {errors[-1]:.2f}. The test uses zero-mean "
        "Gaussian noise and a fixed random seed. The result illustrates the expected effect of "
        "repeated measurements; it does not establish performance on physical sensors. "
        "This is a demonstration publication for the SCI interface.",
        "This reproduction records the random seed, trial count, and calculation used in the "
        "synthetic sensor benchmark. Repeating the computation produces the same output on "
        "the recorded Python version. The test demonstrates how a small signed publication "
        "can carry enough material for an independent check. This is demonstration data.",
        "A synthetic signal with isolated high-amplitude errors provides a counterexample to "
        "a general preference for the mean. The median limits the effect of these spikes "
        "when fewer than half of the readings are affected. This example describes the "
        "assumptions needed to compare estimators. It is demonstration data, not a sensor study.",
    ]
    index = []
    for i in range(3):
        folder = root / f"publication-{i+1}"
        folder.mkdir(exist_ok=True)
        metadata = {
            "title": titles[i], "abstract": abstracts[i],
            "categories": ["cs.LG", "stat.ML"] if i != 1 else ["cs.SE"],
            "keywords": ["synthetic data", "reproducibility", "measurement"],
            "citations": [] if i == 0 else [{
                "target": "sci:" + index[0]["uuid"], "relation": "reproduces" if i == 1 else "uses",
                "reason": "The original benchmark supplies the reference computation and noise assumptions.",
            }],
        }
        text = (
            "## Scope\n\nThis publication uses synthetic data to demonstrate the SCI workflow. "
            "It is not a report about a physical sensor.\n\n"
            "## Method\n\nUse a fixed random seed of `4816`. Generate independent readings from "
            "a normal distribution with mean zero and standard deviation one. For each sample "
            "size, compute the mean of the readings in each of 512 trials. Then compute the "
            "root mean square error across trials.\n\n"
        )
        if i == 0:
            metadata["headline_figure_caption"] = (
                "Root mean square error decreases as more independent readings are averaged. "
                "The line joins the measured values from 512 synthetic trials at each sample size."
            )
            text += ("## Result\n\nThe headline figure shows the decrease in error. The supporting "
                     "CSV contains the plotted values. The test does not include drift, correlated "
                     "noise, or systematic bias.\n\n| Readings | RMSE |\n| --- | --- |\n" +
                     "".join(f"| {n} | {e:.3f} |\n" for n, e in zip(sizes, errors)) +
                     "\n## Reproduce the test\n\nUse `code/benchmark.py` and `data/errors.csv` in the "
                     "supporting ZIP. The file listing is a publisher claim; verification checks "
                     "the signed archive bytes.\n\n")
            (folder / "files" / "data").mkdir(parents=True, exist_ok=True)
            (folder / "files" / "code").mkdir(parents=True, exist_ok=True)
            (folder / "files" / "data" / "errors.csv").write_text(csv)
            (folder / "files" / "code" / "benchmark.py").write_text(
                "import random, statistics, math\n"
                "rng = random.Random(4816)\n"
                "print('samples,rmse')\n"
                "for n in [1,2,4,8,16,32]:\n"
                "    trials = [statistics.mean(rng.gauss(0,1) for _ in range(n)) for _ in range(512)]\n"
                "    print(f'{n},{math.sqrt(statistics.mean(v*v for v in trials)):.6f}')\n"
            )
            figure = Image.new("RGB", (1200, 550), "white")
            draw = ImageDraw.Draw(figure)
            font = ImageFont.load_default(size=20)
            small = ImageFont.load_default(size=17)
            for level in [0, 0.25, 0.5, 0.75, 1.0]:
                y = 440 - level * 340
                draw.line((100, y, 1120, y), fill="#e8e8e8", width=2)
                draw.text((35, y-10), f"{level:.2f}", fill="#666666", font=small)
            points = [(130 + i*190, 440 - e*340) for i, e in enumerate(errors)]
            draw.line(points, fill="#365b78", width=4)
            for (x, y), n in zip(points, sizes):
                draw.ellipse((x-6,y-6,x+6,y+6), fill="#365b78")
                draw.text((x-8,457), str(n), fill="#444444", font=font)
            draw.text((100, 25), "Root mean square error", fill="#242424", font=font)
            draw.text((450, 505), "Independent readings per mean", fill="#666666", font=font)
            draw.text((865, 27), "Synthetic data · 512 trials", fill="#666666", font=small)
            figure.save(folder / "headline_figure.png")
        else:
            text += (
                "## Interpretation\n\nKeep the assumptions with the result. A fixed seed helps "
                "repeat a simulation, but it does not establish that the simulation describes "
                "an experimental system.\n\n"
                "## Limitations\n\nUse independent trials and inspect the noise distribution "
                "before applying this example to measured data.\n\n"
            )
        text += "## Attribution\n\nSCI implementation demonstration. All text, code, and data were prepared for this example.\n"
        (folder / "README.md").write_text(text, "utf-8")
        (folder / "sci.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", "utf-8")
        publication = sign_publication(folder, private)
        index.append({"uuid": publication.uuid, "title": titles[i], "folder": str(folder)})
    (root / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index, public_jwk(load_private(private).public_key())["x"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/demo"))
    parser.add_argument("--push", metavar="INSTANCE_URL")
    args = parser.parse_args()
    index, public = create(args.output)
    print(f"Public key (JWK x): {public}")
    print("Enable this key with sci_private.enable_key before upload.")
    if args.push:
        for item in index:
            print(push(item["folder"], args.push)["url"])


if __name__ == "__main__":
    main()
