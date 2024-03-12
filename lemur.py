#!/usr/bin/env python
#
# Created on 11/07/2023 by Nick Sapoval

import datetime
import argparse
import os
import logging
import sys
import signal
import time
import subprocess as subp
from itertools import repeat
from multiprocessing import Pool

import pandas as pd
import numpy as np
import pysam


__version__ = "1.0.0"


def CTRLChandler(signum, frame):
    global SIGINT
    SIGINT = True
    sys.stderr.write("Caught request to terminate (CTRL+C), exiting...\n")
    sys.exit(128)

signal.signal(signal.SIGINT, CTRLChandler)


class LemurRunEnv():
    GENES = ['RpsE', 'RpsG', 'RpoA', 'RplK', 'RpsQ', 'RplA', 'TsaD', 'RpsK', 'LeuS', 'RpsS',
            'RpsM', 'Ffh', 'RplO', 'SecY', 'ArgS', 'CysS', 'PheS', 'RplB', 'RpsB', 'RplP',
            'RplV', 'RpsO', 'Gtp1', 'RpoB', 'RplD', 'RpsL', 'RplC', 'RpsI', 'RpsD', 'RplM',
            'SerS', 'RplR', 'ValS', 'FtsY', 'RplF', 'RpsH', 'RplN', 'HisS', 'RpsC', 'RplE']
    CIGAR_OPS_CHAR = ["M", "I", "D", "=", "X", "H", "S"]
    CIGAR_OPS_INT  = [ 0,   1,   2,   7,   8,   5,   4 ]
    TAXONOMY_RANKS = ['species', 'genus', 'family', 'order', 'class', 'phylum', 'clade', 'superkingdom']
    FIXED_COSTS    = {0: 1.,
                      1: 0.005,
                      2: 0.005,
                      7: 1.,
                      8: 0.01,
                      4: 0.05,
                      5: 0.001}


    def setup(self):
        self.__init__()


    def __init__(self):
        self.CURRDATE = datetime.date.today().strftime('%Y-%m-%d')
        self.CURRTIME = datetime.date.today().strftime('%H:%M:%S')

        self.args = self.parse_args()
        self.logger = self.logging_setup()

        self.aln_score = self.args.aln_score
        self.by_gene = self.args.aln_score_gene

        if self.args.sam_input:
            self.sam_path = self.args.sam_input
        else:
            self.sam_path = self.args.output + ".sam"

        self.thread_pool = Pool(self.args.num_threads)

        self.tsv_output_path = self.args.output + "-relative_abundance"

        self.lli_threshold = 0.01
        self.low_abundance_threshold = 0.0001

        self.rank = self.args.rank


    def parse_args(self):
        parser = argparse.ArgumentParser(description="""
            Lemur example:
            python lemur.py -i <input> -o <output_dir> -t <threads>
            """)
        
        main_args = parser.add_argument_group(title="Main Mob arguments")
        main_args.add_argument(
            "-i",
            "--input",
            type=str,
            help="Input FASTQ file for the analysis"
        )
        main_args.add_argument(
            "-o",
            "--output",
            type=str,
            default=f"mob_out_{self.CURRDATE}_{self.CURRTIME}",
            help="Folder where the Mob output will be stored"
        )
        main_args.add_argument(
            "-d",
            "--db-prefix",
            type=str,
            help="Path to the folder with individual Emu DBs for each marker gene"
        )
        main_args.add_argument(
            "--tax-path",
            type=str,
            help="Path to the taxonomy.tsv file (common for all DBs)"
        )
        main_args.add_argument(
            "-t",
            "--num-threads",
            type=int,
            default=20,
            help="Number of threads you want to use"
        )
        main_args.add_argument(
            "--aln-score",
            type=str
        )
        main_args.add_argument(
            "--aln-score-gene",
            action="store_true"
        )
        main_args.add_argument(
            "-r",
            "--rank",
            type=str,
            default="species",
        )
        main_args.add_argument(
            "--min-aln-len-ratio",
            type=float,
            default=0.75
        )
        main_args.add_argument(
            "--min-fidelity",
            type=float,
            default=0.50
        )
        main_args.add_argument(
            "--ref-weight",
            type=float,
            default=1.
        )
        main_args.add_argument(
            "--minimap2-AS",
            action="store_true",
        )
        #  Minimap2 specific arguments
        mm2_args = parser.add_argument_group(title="minimap2 arguments")
        mm2_args.add_argument(
            '--mm2-N', type=int, default=50,
            help='minimap max number of secondary alignments per read [50]'
        )
        mm2_args.add_argument(
            '--mm2-K', type=int, default=500000000,
            help='minibatch size for minimap2 mapping [500M]'
        )
        mm2_args.add_argument(
            '--mm2-type', choices=['map-ont', 'map-pb', 'sr'], default='map-ont',
            help='short-read: sr, Pac-Bio: map-pb, ONT: map-ont [map-ont]'
        )
        # Verbosity/logging/additional info
        misc_args = parser.add_argument_group(title="Miscellaneous arguments")
        misc_args.add_argument(
            "--keep-alignments",
            action="store_true",
            help="Keep SAM files after the mapping (might require a lot of disk space)"
        )
        misc_args.add_argument(
            "-e",
            "--log-file",
            type=str,
            default="stdout",
            help="File for logging [default: stdout]"
        )
        misc_args.add_argument(
            "--sam-input",
            type=str,
        )
        misc_args.add_argument(
            "-v",
            "--verbosity",
            type=int,
            default=1,
            help="Logging level: 0 (DEBUG), 1 (INFO), 2 (ERROR)"
        )
        misc_args.add_argument(
            "--save-intermediate-profile",
            action="store_true",
            help="Will save abundance profile at every EM step"
        )
        misc_args.add_argument(
            "--nof",
            action="store_true",
            help="do not apply width filter"
        )
        misc_args.add_argument(
            "--gid-name",
            action="store_true"
        )

        return parser.parse_args()


    def init_taxonomy(self):
        self.df_taxonomy = pd.read_csv(self.args.tax_path, sep='\t', index_col='tax_id', dtype=str)
        self.log(self.df_taxonomy)
        self.log(f"Loaded taxonomy file {self.args.tax_path}")


    def init_F(self):
        self.F = pd.Series(data=[1. / len(self.df_taxonomy)] * len(self.df_taxonomy), 
                           index=self.df_taxonomy.index,
                           name="F")
        self.log(f"Initialized abundance vector")


    def run_minimap2(self):
        db_sequence_file = os.path.join(self.args.db_prefix, 'species_taxid.fasta')

        cmd_str = f"minimap2 -ax map-ont \
                            -N 50 \
                            -p .9 \
                            -f 0 \
                            --sam-hit-only \
                            --eqx \
                            -t {self.args.num_threads} \
                            {db_sequence_file} \
                            {self.args.input} \
                            -o {self.sam_path}"

        p = subp.Popen(cmd_str, 
                    shell=True,
                    stdin=None,
                    stdout=subp.PIPE,
                    stderr=subp.PIPE,
                    close_fds=True,
                    text=True)
        fstdout, fstderr = p.communicate()
        rc = p.returncode

        if (rc != 0):
            self.log(f"minimap2 failed executing:\n{cmd_str}\n\nSTDOUT:{fstdout}\n\nSTDERR:{fstderr}", logging.CRITICAL)
            sys.exit(rc)
        else:
            self.log(fstdout, logging.DEBUG)
            self.log(fstderr, logging.DEBUG)


    def build_alignment_model(self):
        if self.args.minimap2_AS:
            return 
        
        if self.args.aln_score == "markov":
            self.build_transition_mats()
        elif self.args.aln_score == "edit":
            self.edit_cigar = self.build_edit_cost()
        else:
            self.fixed_cigar = {0: 1.,
                                1: 0.005,
                                2: 0.005,
                                7: 1.,
                                8: 0.01,
                                4: 0.05,
                                5: 0.01}


    def build_edit_cost(self):
        op_costs = {0: 0.,  # Match
                    1: 0.,  # Ins
                    2: 0.,  # Del
                    7: 0.,  # Id
                    8: 0.,  # X
                    4: 0.,  # Hard clip
                    5: 0.}  # Soft clip
        ops_w_cost = [1, 2, 8, 4, 5]

        if self.by_gene:
            pass
        else:
            total = 0
            cigars = self.extract_cigars_all(self.sam_path)
            for cigar in cigars:
                for t in cigar:
                    OP, L = t
                    if OP in ops_w_cost:
                        op_costs[OP] += L
                        total += L
            
            for OP in op_costs:
                if OP in ops_w_cost:
                    op_costs[OP] = op_costs[OP] / total
                else:
                    op_costs[OP] = 1.
            
            return op_costs


    def build_transition_mats(self):
        if self.by_gene:
            gene_cigars = self.extract_cigars_per_gene(self.sam_path)
            self.gene_transition_mats = {self.GENES[i]: mat 
                                         for i, mat in enumerate(map(
                                             self.build_transition_mat, gene_cigars.values()
                                             ))
                                        }
        else:
            cigars = self.extract_cigars_all(self.sam_path)
            self.transition_mat = self.build_transition_mat(cigars)


    def extract_cigars_per_gene(self, sam_path):
        samfile = pysam.AlignmentFile(sam_path)
        
        gene_cigars = {gene: [] for gene in self.GENES}
        for read in samfile.fetch():
            if not read.is_secondary and not read.is_supplementary:
                gene_cigars[read.reference_name.split(":")[1].split("/")[-1]].append(read.cigartuples)

        return gene_cigars


    def extract_cigars_all(self, sam_path):
        samfile = pysam.AlignmentFile(sam_path)
        
        cigars = []
        for read in samfile.fetch():
            if not read.is_secondary and not read.is_supplementary:
                cigars.append(read.cigartuples)

        return cigars


    def build_transition_mat(self, cigars):
        transition_mat = pd.DataFrame(data=[[0] * (len(self.CIGAR_OPS_INT) + 1) \
                                            for _ in range(len(self.CIGAR_OPS_INT) + 1)], 
                                      index=self.CIGAR_OPS_INT + [-1], 
                                      columns=self.CIGAR_OPS_INT + [-1])

        for cigar in cigars:
            N = len(cigar)
            prev_OP = None
            for i, t in enumerate(cigar):
                OP, L = t
                if not ((i == 0 or i == N - 1) and (OP == self.CIGAR_OPS_INT[-1])):
                    if OP in self.CIGAR_OPS_INT:
                        transition_mat.at[OP, OP] += L - 1
                        if not prev_OP is None:
                            transition_mat.at[prev_OP, OP] += 1
                        prev_OP = OP
            # transition_mat.at[prev_OP, -1] += 1

        transition_mat = transition_mat.div(transition_mat.sum(axis=1), axis=0).fillna(0.)
        self.log(transition_mat, logging.DEBUG)
        
        return transition_mat


    @staticmethod
    def __get_aln_len(aln):
        _, I, _, _, _, _, _, E, X, _, _ = aln.get_cigar_stats()[0]
        return I + E + X


    def build_P_rgs_df(self):
        samfile = pysam.AlignmentFile(self.sam_path)

        P_rgs_data = {"Read_ID": [], "Target_ID": [], "log_P": [], "cigar": [], "Gene": [], "Reference": [], "aln_len": []}

        for i, aln in enumerate(samfile.fetch()):
            qname = aln.query_name
            aln_score = aln.get_tag("AS")
            if aln_score > 0:
                if self.args.gid_name:
                    species_tid = aln.reference_name.rsplit("_", maxsplit=1)[0]
                else:
                    species_tid = int(aln.reference_name.split(":")[0])
                    gene = aln.reference_name.split(":")[1].split("/")[-1]
                cigar = aln.cigartuples

                if self.args.minimap2_AS:
                    log_P_score = np.log(aln_score / (2 * self.__get_aln_len(aln)))
                    P_rgs_data["log_P"].append(log_P_score)

                P_rgs_data["Read_ID"].append(qname)
                P_rgs_data["Target_ID"].append(species_tid)
                if not self.args.minimap2_AS:
                    P_rgs_data["cigar"].append(cigar)
                P_rgs_data["Gene"].append(gene)
                P_rgs_data["Reference"].append(aln.reference_name)
                P_rgs_data["aln_len"].append(self.__get_aln_len(aln))

            if (i + 1)% 100000 == 0:
                self.log(f"build_P_rgs_df extracted {i+1} reads", logging.DEBUG)


        if self.args.minimap2_AS:
            pass
        elif self.aln_score == "markov":
            if self.by_gene:
                cigar_gene_mat_tuples = [(cigar, self.gene_transition_mats[P_rgs_data["Gene"][i]]) \
                                         for i, cigar in enumerate(P_rgs_data["cigar"])]
                P_rgs_data["log_P"] = self.thread_pool.starmap(self.score_cigar_markov,
                                                               cigar_gene_mat_tuples)
            else:
                P_rgs_data["log_P"] = self.thread_pool.starmap(self.score_cigar_markov,
                                                               zip(P_rgs_data["cigar"], 
                                                               repeat(self.transition_mat)))
        elif self.aln_score == "edit":
            if self.by_gene:
                log_P_func = self.score_cigar_fixed(P_rgs_data["cigar"][i], self.gene_edit_cigars[gene])
            else:
                P_rgs_data["log_P"] = self.thread_pool.starmap(self.score_cigar_fixed,
                                                               zip(P_rgs_data["cigar"], 
                                                               repeat(self.edit_cigar)))
        else:
            if self.by_gene:
                log_P_func = self.score_cigar_fixed(P_rgs_data["cigar"][i], self.gene_fixed_cigars[gene])
            else:
                P_rgs_data["log_P"] = self.thread_pool.starmap(self.score_cigar_fixed,
                                                               zip(P_rgs_data["cigar"], 
                                                               repeat(self.fixed_cigar)))

        del P_rgs_data["cigar"]

        self.P_rgs_df = pd.DataFrame(data=P_rgs_data)
        self.P_rgs_df["max_aln_len"] = self.P_rgs_df.groupby("Read_ID")["aln_len"].transform('max')
        self.P_rgs_df["log_P"] = self.P_rgs_df["log_P"] * self.P_rgs_df["max_aln_len"] / self.P_rgs_df["aln_len"]
        self.P_rgs_df["max_log_P"] = self.P_rgs_df.groupby("Read_ID")["log_P"].transform('max')

        self.P_rgs_df.to_csv(f"{self.args.output}_P_rgs_df_raw.tsv", sep='\t', index=False)

        self.gene_stats_df = pd.read_csv(f"{self.args.db_prefix}/gene2len.tsv", sep='\t')
        self.gene_stats_df["type"] = self.gene_stats_df["#id"].str.split("_").str[-1].str.split(":").str[0]
        self.gene_stats_df["Target_ID"] = self.gene_stats_df["#id"].str.split(":").str[0].astype(int)
        gene_p_rgs_len_df = self.P_rgs_df.merge(self.gene_stats_df,
                                                how="left", right_on="#id", left_on="Reference")
        gene_p_rgs_len_df["aln_len_ratio"] = gene_p_rgs_len_df["aln_len"] / gene_p_rgs_len_df["length"]
        gene_p_rgs_len_df["fidelity"] = gene_p_rgs_len_df["log_P"] / gene_p_rgs_len_df["aln_len"]

        gene_p_rgs_len_df["ref_len_weighted_log_P"] = gene_p_rgs_len_df["log_P"] + \
                                                      self.args.ref_weight * np.log(gene_p_rgs_len_df["aln_len_ratio"])
        gene_p_rgs_len_df.to_csv(f"{self.args.output}_gene_P_rgs_df_raw.tsv", sep='\t', index=False)
        if self.args.ref_weight != 0:
            self.P_rgs_df["log_P"] = gene_p_rgs_len_df["ref_len_weighted_log_P"]

        if self.args.minimap2_AS:
            self.P_rgs_df = self.P_rgs_df[(gene_p_rgs_len_df["aln_len_ratio"] >= self.args.min_aln_len_ratio) &
                                          (gene_p_rgs_len_df["log_P"]>=1.1*gene_p_rgs_len_df["max_log_P"]) & 
                                          (gene_p_rgs_len_df["log_P"]>=np.log(self.args.min_fidelity))]
            gene_p_rgs_len_df = gene_p_rgs_len_df[(gene_p_rgs_len_df["aln_len_ratio"] >= self.args.min_aln_len_ratio) &
                                                  (gene_p_rgs_len_df["log_P"]>=1.1*gene_p_rgs_len_df["max_log_P"]) & 
                                                  (gene_p_rgs_len_df["log_P"]>=np.log(self.args.min_fidelity))]
        else:
            self.P_rgs_df = self.P_rgs_df[(gene_p_rgs_len_df["aln_len_ratio"] >= self.args.min_aln_len_ratio) &
                                          (gene_p_rgs_len_df["fidelity"] >= self.args.min_fidelity)]
            gene_p_rgs_len_df = gene_p_rgs_len_df[(gene_p_rgs_len_df["aln_len_ratio"] >= self.args.min_aln_len_ratio) &
                                                  (gene_p_rgs_len_df["fidelity"] >= self.args.min_fidelity)]

        ref2genome_df = pd.read_csv(f"{self.args.db_prefix}/reference2genome.tsv", sep='\t', 
                                    names=["Name", "Genome"])
        self.P_rgs_df = self.P_rgs_df.merge(ref2genome_df, how="left", left_on="Reference", right_on="Name")

        self.P_rgs_df = self.P_rgs_df.groupby(by=["Read_ID", "Target_ID"]).max()
        __df_id_count = self.P_rgs_df.reset_index().groupby("Read_ID").count().reset_index()[["Read_ID", "Target_ID"]]
        __df_id_count.columns = ["Read_ID", "Map_Count"]
        __df = self.P_rgs_df.reset_index().merge(__df_id_count, how="left", on="Read_ID")
        self.unique_mapping_support_tids = set(__df[__df["Map_Count"]==1]["Target_ID"])
        self.log(self.P_rgs_df)
        self.P_rgs_df.reset_index().to_csv(f"{self.args.output}_P_rgs_df.tsv", sep='\t', index=False)
        return self.P_rgs_df


    def logging_setup(self):
        logger = logging.getLogger("Mob")
        logger.setLevel(logging.DEBUG)
        
        if self.args.log_file == "stdout":
            handler = logging.StreamHandler(stream=sys.stdout)
        elif self.args.log_file == "stderr":
            handler = logging.StreamHandler(stream=sys.stderr)
        else:
            handler = logging.FileHandler(self.args.log_file, mode='w')
        
        error_handler = logging.StreamHandler(stream=sys.stderr)
        error_handler.setLevel(logging.ERROR)

        if self.args.verbosity < 1:
            handler.setLevel(logging.DEBUG)
        elif self.args.verbosity == 1:
            handler.setLevel(logging.INFO)
        else:
            handler.setLevel(logging.ERROR)

        formatter = logging.Formatter("%(asctime)s %(message)s",
                                      datefmt="%Y-%m-%d %I:%M:%S %p")
        handler.setFormatter(formatter)
        error_handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.addHandler(error_handler)
        return logger


    def log(self, msg, level=logging.DEBUG):
        if level == logging.DEBUG:
            self.logger.debug(msg)
        elif level == logging.INFO:
            self.logger.info(msg)
        elif level == logging.WARNING:
            self.logger.warning(msg)
        elif level == logging.ERROR:
            self.logger.error(msg)
        elif level == logging.CRITICAL:
            self.logger.error(msg)
    

    @staticmethod
    def score_cigar_markov(cigar, T):
        N = len(cigar)
        prev_OP = None
        log_P = 0.
        for i, t in enumerate(cigar):
            OP, L = t
            if not ((i == 0 or i == N - 1) and (OP == LemurRunEnv.CIGAR_OPS_INT[-1])):
                if OP in [1, 2, 7, 8, 5, 4]:
                    if T.at[OP, OP] != 0:
                        log_P += (L - 1) * np.log(T.at[OP, OP])
                    else:
                        log_P += (L - 1) * np.log(LemurRunEnv.FIXED_COSTS[OP])
                    
                    if not prev_OP is None:
                        if T.at[prev_OP, OP] != 0:
                            log_P += np.log(T.at[prev_OP, OP])

                    prev_OP = OP
                
        # log_P += np.log(T.at[prev_OP, -1])
        return log_P


    @staticmethod
    def score_cigar_fixed(cigar, fixed_op_cost):
        N = len(cigar)
        aln_cigar = [ct for i, ct in enumerate(cigar)] # if not ((i == 0 or i == N - 1) and (ct[0] == LemurRunEnv.CIGAR_OPS_INT[-1]))]
        log_P = sum(map(lambda OP_L: np.log(fixed_op_cost[OP_L[0]]) * OP_L[1], aln_cigar))
        return log_P


    @staticmethod
    def logSumExp(ns):
        __max = np.max(ns)
        if not np.isfinite(__max):
            __max = 0
        ds = ns - __max
        with np.errstate(divide='ignore'):
            sumOfExp = np.exp(ds).sum()
        return __max + np.log(sumOfExp)    


    def EM_step(self, final=False):
        if final:
            self.F = self.final_F
        self.P_tgr = self.P_rgs_df.reset_index().merge(self.F,
                                                       how="inner", 
                                                       left_on="Target_ID", 
                                                       right_index=True)
        self.P_tgr["P(r|t)*F(t)"] = self.P_tgr.log_P + np.log(self.P_tgr.F)
        self.P_tgr_sum = self.P_tgr[["Read_ID", "P(r|t)*F(t)"]].groupby(by="Read_ID", group_keys=False) \
                                                               .agg(self.logSumExp)
        self.P_tgr = self.P_tgr.merge(self.P_tgr_sum,
                                      how="left",
                                      left_on="Read_ID",
                                      right_index=True,
                                      suffixes=["", "_sum"])
        self.P_tgr["P(t|r)"] = self.P_tgr["P(r|t)*F(t)"] - self.P_tgr["P(r|t)*F(t)_sum"]

        self.log(set(self.P_tgr["Target_ID"]), logging.DEBUG) 

        n_reads = len(self.P_tgr_sum)

        self.F = self.P_tgr[["Target_ID", "P(t|r)"]].groupby("Target_ID") \
                                                    .agg(lambda x: np.exp(LemurRunEnv.logSumExp(x) - np.log(n_reads)))["P(t|r)"]
        self.F.name = "F"
        self.F = self.F.loc[self.F!=0]
        self.log(self.F.sum(), logging.DEBUG)

        if final:
            self.final_F = self.F


    def compute_loglikelihood(self):
        return self.P_tgr["P(r|t)*F(t)_sum"].sum()
    

    def EM_complete(self):
        n_reads = len(set(self.P_rgs_df.reset_index()["Read_ID"]))
        self.low_abundance_threshold = 1. / n_reads
        
        if not self.args.nof:
            __P_rgs_df = self.P_rgs_df.reset_index()
            tids = list(self.F.index)
            filter_pass = []
            for tid in tids:
                tid_df = __P_rgs_df[__P_rgs_df["Target_ID"] == tid]
                N_genes_hit = len(set(tid_df["Gene"]))
                N_genes = len(set(self.gene_stats_df[self.gene_stats_df["Target_ID"] == tid]["type"]))
                N_reads = len(set(tid_df["Read_ID"]))
                if N_reads > 10:
                    E_N_genes_hit, variance = LemurRunEnv.get_expected_gene_hits(N_genes, N_reads)
                        # For small number of reads mapped, i.e. 4-8 ratio is helpful since 6 out of 8 works
                    if (N_genes_hit / E_N_genes_hit > 0.7 or \
                        # For "large" numbers like 20 MGs at 49 reads we can check by variance; formally as reads -> infinity, the variance is more informative
                        E_N_genes_hit - N_genes_hit <= 3 * variance) \
                        and N_genes_hit > 1:
                        filter_pass.append(True)
                    else:
                        filter_pass.append(False)
                else:
                    if N_reads == 0:
                        filter_pass.append(False)
                    else:
                        filter_pass.append(True)
            self.F = self.F.loc[filter_pass]

        lli = -np.inf
        i = 1
        while True:
            self.log(f"Starting EM iteration {i}", logging.DEBUG)
            self.EM_step()
            new_lli = self.compute_loglikelihood()

            lli_delta = new_lli - lli
            self.log(f"Current log likelihood {new_lli}", logging.DEBUG)
            self.log(f"LLI delta is {lli_delta}", logging.DEBUG)
            lli = new_lli

            self.first_EM_df = self.df_taxonomy.merge(self.F, how='right', left_index=True, right_index=True).reset_index()

            if self.args.save_intermediate_profile:
                intermediate_df = self.df_taxonomy.merge(self.F, how='right', left_index=True, right_index=True).reset_index()
                intermediate_df.to_csv(f"{self.tsv_output_path}-EM-{i}.tsv", sep='\t', index=False)

            if lli_delta < self.lli_threshold:
                self.log(f"Low abundance threshold: {self.low_abundance_threshold:.8f}", logging.INFO)
                # self.final_F = self.F.loc[(self.F>=self.low_abundance_threshold) &
                #                           (self.F.index.isin(self.unique_mapping_support_tids))]

                # 02/07/2024
                # __P_rgs_df = self.P_rgs_df.reset_index()
                # tids = list(self.F.index)
                # filter_pass = []
                # for tid in tids:
                #     tid_df = __P_rgs_df[__P_rgs_df["Target_ID"] == tid]
                #     N_genes_hit = len(set(tid_df["Gene"]))
                #     N_genes = len(set(self.gene_stats_df[self.gene_stats_df["Target_ID"] == tid]["type"]))
                #     N_reads = len(set(tid_df["Read_ID"]))
                #     E_N_genes_hit, variance = LemurRunEnv.get_expected_gene_hits(N_genes, N_reads)
                #         # For small number of reads mapped, i.e. 4-8 ratio is helpful since 6 out of 8 works
                #     if (N_genes_hit / E_N_genes_hit > 0.7 or \
                #         # For "large" numbers like 20 MGs at 49 reads we can check by variance; formally as reads -> infinity, the variance is more informative
                #         E_N_genes_hit - N_genes_hit <= 3 * variance) \
                #         and N_genes_hit > 1:
                #         filter_pass.append(True)
                #     else:
                #         filter_pass.append(False)
                # self.final_F = self.F.loc[filter_pass & (self.F>=self.low_abundance_threshold)]
                self.final_F = self.F.loc[self.F>=self.low_abundance_threshold]
                # ----------

                self.EM_step(final=True)

                self.freq_to_lineage_df()
                self.collapse_rank()

                return
            
            i += 1
            

    @staticmethod
    def get_expected_gene_hits(N_genes, N_reads):
        E = N_genes * (1 - np.power(1 - 1 / N_genes, N_reads)) 
        V = N_genes * np.power(1 - 1 / N_genes, N_reads) + \
            N_genes * N_genes * (1 - 1 / N_genes) * np.power(1 - 2 / N_genes, N_reads) - \
            N_genes * N_genes * np.power((1 - 1 / N_genes), 2 * N_reads)
        return E, V


    def freq_to_lineage_df(self):
        results_df = self.df_taxonomy.merge(self.final_F, how='right', left_index=True, right_index=True).reset_index()
        results_df.to_csv(f"{self.tsv_output_path}.tsv", sep='\t', index=False)
        
        return results_df


    def collapse_rank(self):
        df_emu = pd.read_csv(f"{self.tsv_output_path}.tsv", sep='\t')
        if self.rank not in self.TAXONOMY_RANKS:
            raise ValueError("Specified rank must be in list: {}".format(self.TAXONOMY_RANKS))
        keep_ranks = self.TAXONOMY_RANKS[self.TAXONOMY_RANKS.index(self.rank):]
        for keep_rank in keep_ranks:
            if keep_rank not in df_emu.columns:
                keep_ranks.remove(keep_rank)
        if "estimated counts" in df_emu.columns:
            df_emu_copy = df_emu[['F', 'estimated counts'] + keep_ranks]
            df_emu_copy = df_emu_copy.replace({'-': 0})
            df_emu_copy = df_emu_copy.astype({'F': 'float', 'estimated counts': 'float'})
        else:
            df_emu_copy = df_emu[['F'] + keep_ranks]
            df_emu_copy = df_emu_copy.replace({'-': 0})
            df_emu_copy = df_emu_copy.astype({'F': 'float'})
        df_emu_copy = df_emu_copy.groupby(keep_ranks, dropna=False).sum()
        output_path = f"{self.tsv_output_path}-{self.rank}.tsv"
        df_emu_copy.to_csv(output_path, sep='\t')
        self.log(df_emu_copy.nlargest(30, ["F"]), logging.DEBUG)
        self.log(f"File generated: {output_path}\n", logging.DEBUG)


def main():
    run = LemurRunEnv()

    if not run.args.sam_input:
        ts = time.time_ns()
        run.run_minimap2()
        t0 = time.time_ns()
        run.log(f"Finished running minimap2 in {(t0-ts)/1000000.0:.3f} ms", logging.INFO)

    t0 = time.time_ns()
    run.init_taxonomy()
    t1 = time.time_ns()
    run.log(f"Finished loading taxonomy in {(t1-t0)/1000000.0:.3f} ms", logging.INFO)
    
    run.init_F()
    t2 = time.time_ns()
    run.log(f"Finished initializing F in {(t2-t1)/1000000.0:.3f} ms", logging.INFO)

    run.build_alignment_model()
    t3 = time.time_ns()
    run.log(f"Finished building alignment model in {(t3-t2)/1000000.0:.3f} ms", logging.INFO)

    run.build_P_rgs_df()
    t4 = time.time_ns()
    run.log(f"Finished constructing P(r|s) in {(t4-t3)/1000000.0:.3f} ms", logging.INFO)

    run.EM_complete()
    t5 = time.time_ns()
    run.log(f"Finished EM in {(t5-t4)/1000000.0:.3f} ms", logging.INFO)

    run.freq_to_lineage_df()
    run.collapse_rank()

    if not run.args.keep_alignments:
        os.remove(run.sam_path)

    exit(0)


if __name__ == "__main__":
    main()
