"""Train the diagnostic model on simulated curves and benchmark it against the rules.

Run:  python train.py            (about 1-2 minutes)
"""
import sys

from sn.model import train

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    train(n_per_class=n)
