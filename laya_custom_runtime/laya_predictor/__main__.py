import argparse
import kserve

from .laya_predictor import LayaPredictor

parser = argparse.ArgumentParser(parents=[kserve.model_server.parser])
parser.add_argument(
    "--device",
    default=None,
    help="Device Laya checkpoints run on (cuda|cpu|mps). Defaults to Laya's own detection.",
)
parser.add_argument(
    "--default",
    default="english",
    help="Checkpoint the Router falls back to when routing can't identify the language.",
)
parser.add_argument(
    "--max_loaded",
    type=int,
    default=2,
    help="Maximum number of checkpoints kept resident at once (LRU eviction).",
)
parser.add_argument(
    "--preload",
    dest="preload",
    action="store_true",
    default=True,
    help="Load all three checkpoints at startup instead of lazily on first use (default: on).",
)
parser.add_argument(
    "--no-preload",
    dest="preload",
    action="store_false",
    help="Disable preloading and load checkpoints lazily on first use.",
)

args, _ = parser.parse_known_args()

if __name__ == "__main__":
    model = LayaPredictor(
        name=args.model_name,
        device=args.device,
        default=args.default,
        max_loaded=args.max_loaded,
        preload=args.preload,
    )
    model.load()
    kserve.ModelServer().start(models=[model])
