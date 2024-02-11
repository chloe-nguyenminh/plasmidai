import torch

LETTER_TO_BASES = {
    "A": "A",
    "B": "CGT",
    "C": "C",
    "D": "AGT",
    "G": "G",
    "H": "ACT",
    "K": "GT",
    "M": "AC",
    "N": "ACGT",
    "R": "AG",
    "S": "CG",
    "T": "T",
    "V": "ACG",
    "W": "AT",
    "Y": "CT",
}

INDEX_TO_BASE = "ACGT"
BASE_TO_INDEX = {b: i for i, b in enumerate(INDEX_TO_BASE)}


def dna_to_tensor(dna):
    sequence = []
    for x in dna:
        choices = LETTER_TO_BASES[x]
        i = torch.randint(len(choices), size=[1]).item()
        base = choices[i]
        sequence.append(BASE_TO_INDEX[base])
    return torch.tensor(sequence, dtype=torch.long)


def tensor_to_dna(sequence, eos):
    assert sequence.ndim == 1
    assert eos not in BASE_TO_INDEX.values()
    dna = []
    for idx in sequence:
        idx = idx.item()
        if idx == eos:
            break
        dna.append(INDEX_TO_BASE[idx])
    return "".join(dna)


def random_roll(sequence):
    assert sequence.ndim == 1
    shift = torch.randint(sequence.shape[0], size=[1]).item()
    return torch.roll(sequence, shifts=shift, dims=0)