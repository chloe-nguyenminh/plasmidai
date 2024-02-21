# Data

## Download

Download the full-length plasmid dataset:

```bash
gdown "1iIsat00ST5vK-06BUstuTbJkfWKpV2lE" 
```

Unzip and rename it:

```bash 
gzip -d 240212_plasmid_seq_54646.fasta.gz
mv 240212_plasmid_seq_54646.fasta plasmids.fasta
```

Everything below is **optional** since the `splits.csv` file is uploaded.
 
## Clustering plasmids

Some plasmids are too long to cluster in reasonable time, so we will only try to cluster those <15 Kbp. This is
reasonable since we are interested in <10 Kbp *E. coli* plasmids. First, filter the plasmids by length:

```bash
python scripts/filter.py --path=plasmids.fasta --L=15000
```

Cluster the filtered plasmids with MMseqs2:

```bash
mmseqs createdb plasmids.short.fasta DB
mmseqs cluster DB DB_clu tmp --min-seq-id 0.8 --wrapped-scoring 1 --cov-mode 3 --cluster-reassign
mmseqs createtsv DB DB DB_clu DB_clu.tsv 
```

## Creating dataset splits

Partition plasmids into training, validation, and test (80:10:10) sets:

```bash 
python scripts/partition.py --path=plasmids.fasta --clusters=DB_clu.tsv --out=splits.csv
```