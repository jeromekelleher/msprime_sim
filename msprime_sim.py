#!/usr/bin/env python

from __future__ import division
import msprime
import numpy as np
from tqdm import tqdm
import matplotlib.pyplot as plt
import os
import sys
import math
import time, sys, traceback, argparse
import pandas as pd
import src.printing as pr
import src.msprime_sim_scenarios as mssim
import src.sumstats as sumstats
import src.regressions as reg
import src.phenotypes as ph
import src.pcgc as pcgc
import src.tools as tl
import src.ldscores as ld
import src.snpgetter as sg
import src.tree_sequence as ts
import statsmodels.api as sm
import tqdm
import tempfile
import scipy.stats as sp
import random
import src.write as write

def simulate_tree_and_betas(args, log):

	args, rec_map_list = tl.initial_warnings_and_parsing(args, log)

	h2_ldsc = np.zeros(args.n_sims, dtype=np.dtype([
		('h2_A', float), ('int_A', float),
		('h2_D', float), ('int_D', float),
		('h2_AC', float), ('int_AC', float)]))
	h2_pcgc = np.zeros(args.n_sims, dtype=np.dtype([
		('h2_A', float), ('h2_D', float), ('h2_AC', float)]))
	h2_ldsc_int = np.zeros(args.n_sims, dtype=np.dtype([
		('h2_A', float), ('int_A', float),
		('h2_D', float), ('int_D', float),
		('h2_AC', float), ('int_AC', float)]))

	for sim in range(args.n_sims):
		if (sim == 0 or args.fix_genetics is False):
			if args.load_tree_sequence is None:
				tree_sequence_list, tree_sequence_list_geno, m, m_start, m_total, m_geno, m_geno_start, m_geno_total, N, n_pops  = ts.simulate_tree_sequence(args, rec_map_list, log)
				if args.dump_trees:
					for chr in range(args.n_chr):
						dump_out = args.out + ".chr" + str(chr+1) + ".sim" + str(sim+1) + ".tree"
						tree_sequence_list[chr].dump(dump_out)
			else:
				tree_sequence_list, tree_sequence_list_geno, m, m_start, m_total, m_geno, m_geno_start, m_geno_total, N, n_pops = ts.load_tree_sequence(args, log)

			# If we don't sample to get estimates of the LD scores, or run case control, then if fix_genetics is used we only need to calculate the LD scores once.
			if args.ldsc and (args.ldscore_within_sample is False or args.case_control is False): # DEV: Currently we have no ascertainment for continuous traits coded up.
				# If the optional argument to obtain LD scores from a random sub-sample of the population, 
				# set the indexing of this sub-sampling here.
				if args.ldscore_sampling_prop is None:
					ldsc_index = None
					n_ldsc = N
				else:
					log.log('Using a subset of individuals from the sampled tree to determine LD scores - LD score sampling proportion: {ld}'.format(ld=args.ldscore_sampling_prop))
					n_ldsc = int(N*args.ldscore_sampling_prop)
					ldsc_index = random.sample(range(N), n_ldsc)

				lN_A, lN_D = ld.get_ldscores(args, m_geno, m_geno_start, m_geno_total, tree_sequence_list_geno, n_ldsc, ldsc_index, sim, log)

		# Note that we pass the tree_sequence_list as potentially non-genotyped SNPs affect phenotype.
		y, C = ph.get_phenotypes(args, N, n_pops, tree_sequence_list, m_total, log)
		log.log('Evaluated phenotypes.')

		# Need to alter this function - splitting further.
		if args.ldsc:
			if args.case_control:
				chisq_A, chisq_D, chisq_AC, n, C_sim, index, y_cc, n_cases, T = ph.get_chisq(args, tree_sequence_list_geno, m_geno, m_geno_total, y, N, C, log)
				study_prevalence = n_cases / n
				scaling = (args.prevalence**2 * (1-args.prevalence)**2) / ((study_prevalence * (1 - study_prevalence)) * sp.norm.pdf(T)**2)
				# If we sample individuals to estimate LD scores from, or run a case-control analysis, we need to regenerate the LD scores.
				if args.ldscore_within_sample:
					if args.ldscore_sampling_prop is None:
						ldsc_index = index
						n_ldsc = n
					else:
						log.log('Using a subset of individuals from the sampled tree to determine LD scores - LD score sampling proportion: {ld}'.format(ld=args.ldscore_sampling_prop))
						n_ldsc = int(n*args.ldscore_sampling_prop)
						ldsc_index = random.sample(index, n_ldsc)

					lN_A, lN_D = ld.get_ldscores(args, m_geno, m_geno_start, m_geno_total, tree_sequence_list_geno, n_ldsc, ldsc_index, sim, log)
			else:
				chisq_A, chisq_D, chisq_AC, n, C_sim, index = ph.get_chisq(args, tree_sequence_list_geno, m_geno, m_geno_total, y, N, C, log)
				scaling = 1

			# Intercept options for the regression.
			intercept_h2 = [None]
			if args.free_and_no_intercept: intercept_h2 = [None, 1]
			if args.no_intercept: intercept_h2 = [1]

			# Run the regressions
			log.log('Running LD score regressions.')
			hsqhat_A, hsqhat_D, hsqhat_AC = [], [], []

			for i in range(len(intercept_h2)):
				hsqhat_A.append(reg.Hsq(chisq_A,
					lN_A.reshape((m_geno_total,1)), lN_A.reshape((m_geno_total,1)),
					np.tile(n,m_geno_total).reshape((m_geno_total,1)), np.array(m_geno_total).reshape((1,1)),
					n_blocks = min(m_geno_total, args.n_blocks), intercept = intercept_h2[i]))

				hsqhat_D.append(reg.Hsq(chisq_D,
					lN_D.reshape((m_geno_total,1)), lN_D.reshape((m_geno_total,1)),
					np.tile(n,m_geno_total).reshape((m_geno_total,1)), np.array(m_geno_total).reshape((1,1)),
					n_blocks = min(m_geno_total, args.n_blocks), intercept = intercept_h2[i]))

				hsqhat_AC.append(reg.Hsq(chisq_AC,
					lN_A.reshape((m_geno_total,1)), lN_A.reshape((m_geno_total,1)),
					np.tile(n,m_geno_total).reshape((m_geno_total,1)), np.array(m_geno_total).reshape((1,1)),
					n_blocks = min(m_geno_total, args.n_blocks), intercept = intercept_h2[i]))

		if args.pcgc:
			if args.case_control:
				y = (y_cc - np.mean(y_cc)) / np.std(y_cc)
			pcgc.pcgc(args, sim, tree_sequence_list_geno, y, h2_pcgc, n, C_sim, index, m_geno_total, scaling, log)

		if args.ldsc is True:
			h2_ldsc['h2_A'][sim], h2_ldsc['h2_D'][sim], h2_ldsc['h2_AC'][sim] = np.array([hsqhat_A[0].tot, hsqhat_D[0].tot, hsqhat_AC[0].tot]) * scaling
			h2_ldsc['int_A'][sim], h2_ldsc['int_D'][sim], h2_ldsc['int_AC'][sim] = hsqhat_A[0].intercept, hsqhat_D[0].intercept, hsqhat_AC[0].intercept

			if args.free_and_no_intercept:
				h2_ldsc_int['h2_A'][sim], h2_ldsc_int['h2_D'][sim], h2_ldsc_int['h2_AC'][sim] = np.array([hsqhat_A[1].tot, hsqhat_D[1].tot, hsqhat_AC[1].tot]) * scaling
				h2_ldsc_int['int_A'][sim], h2_ldsc_int['int_D'][sim], h2_ldsc_int['int_AC'][sim] = hsqhat_A[1].intercept, hsqhat_D[1].intercept, hsqhat_AC[1].intercept

	return h2_ldsc, h2_pcgc, h2_ldsc_int

parser = argparse.ArgumentParser()
parser.add_argument('--out', default='msprimesim', type=str,
    help='Output filename prefix. This will be an output of heritability estimates'
    ' across the n_sims.')
parser.add_argument('--h2_A', default=0.3, type=float,
	help='Additive heritability contribution [Default: 0.3].')
parser.add_argument('--h2_D', default=0.1, type=float,
	help='Dominance heritability contribution [Default: 0.1].')
parser.add_argument('--h2_AC', default=0.2, type=float,
	help='Dominance heritability contribution [Default: 0.2].')
parser.add_argument('--C-bool', default=False, action='store_true',
	help='Is the environmental covariate that you consider boolean?')
parser.add_argument('--C-bool-p', default=0.5, type=float,
	help='Probability of "success" for a boolean covariate which is independent of the genetic data.')
# DEV: Shouldn't there be the possibility for this to be different across the different classes?
parser.add_argument('--p-causal', default=1, type=float,
	help='Proportion of SNPs that are causal [Default: 1].')
# Filtering / Data Management for LD Score
parser.add_argument('--ld-wind-snps', default=1000, type=int,
    help='Specify the window size to be used for estimating LD Scores in units of '
    '# of SNPs [Default: 1000].')
# Flags you should almost never use
parser.add_argument('--chunk-size', default=50, type=int,
    help='Chunk size for LD Score calculation. Use the default [Default: 50].')
parser.add_argument('--n', default=40000, type=int,
	help='Number of individuals in the sampled genotypes [Default: 40,000].')
parser.add_argument('--m', default=1000000, type=int,
	help='Length of the region analysed in nucleotides [Default: 1,000,000].')
parser.add_argument('--Ne', default=10000, type=int,
	help='Effective population size [Default: 10,000].')
parser.add_argument('--sim-type', default='standard', type=str,
    help='Type of simulation to run. Currently recognises "standard",'
    '"out-of-africa" and "out-of-africa-all-pops" [Default: standard]')
parser.add_argument('--maf', default=0.05, type=float,
    help='The minor allele frequency cut-off [Default: 0.05].')
parser.add_argument('--rec', default=2e-8, type=float,
    help='Recombination rate across the region [Default: 2e-8].')
parser.add_argument('--mut', default=2e-8, type=float,
	help='Mutation rate across the region [Default: 2e-8].')
parser.add_argument('--no-intercept', action='store_true',
    help = 'This constrains the LD Score regression intercept to equal 1.')
parser.add_argument('--free-and-no-intercept', action='store_true',
    help = 'This runs both a free and a constrained intercept equal to 1.')
parser.add_argument('--n-sims', default=1, type=int,
	help='Number of msprime simulations to run [Default: 1].')
parser.add_argument('--n-blocks', default=200, type=int,
	help='Number of block jackknife blocks [Default: 200].')
parser.add_argument('--fix-genetics', default=False, action='store_true',
	help='Fix the genetic data to be the same across runs.')
parser.add_argument('--n-chr', default=1, type=int,
	help='Number of chromosomes to simulate if --sim-chr is set [Default: 1].')
parser.add_argument('--rec-map', default=None, type=str,
	help='If you want to pass a recombination map, include the filepath here.')
parser.add_argument('--rec-map-chr', default=None, type=str,
	help='If you want to pass a recombination map, include the filepath here. '
	'The filename should contain the symbol @, msprimesim will replace instances '
	'of @ with chromosome numbers.')
parser.add_argument('--pcgc', default=False, action='store_true',
	help='Do you want to estimate heritability using PCGC too? Warning: slow and memory intensive.')
parser.add_argument('--dominance', default=False, action='store_true',
	help='Do you want to include dominance simulation and estimation?')
parser.add_argument('--gxe', default=False, action='store_true',
	help='Do you want to include a gene by environment interaction simulation and estimation?')
parser.add_argument('--write-trees', default=False, action='store_true',
	help='Do you want to write the tree sequences to .bim/.bed/.fam format?')
parser.add_argument('--geno-prop', default=None, type=float,
	help='Is the proportion of SNPs genotyped different to the number of SNPs?')
parser.add_argument('--progress-bars', default=False, action='store_true',
	help='Do you want fancy progress bars? Important - Don\'t use this flag when running jobs on a '
	'cluster, lest you want tons of printing to your .log file!')
parser.add_argument('--same-causal-sites', default=False, action='store_true', 
	help='Are the causal sites the same across additive, dominance, and gene x environment components?')
parser.add_argument('--case-control', default=False, action='store_true', 
	help='Do you want to run a case-control GWAS?')
parser.add_argument('--prevalence', default=0.1, type=float,
	help='If running a case-control GWAS, what is the prevalence of the disorder? [Default: 0.1]')
parser.add_argument('--linear', default=False, action='store_true',
	help='Do you want to run linear regression when using the case-control flag?')
parser.add_argument('--n-cases', default=1000, type=float,
	help='If running a case-control study simulation, the expected number of cases given the provided prevalence '
	'in the population [Default: 1000].')
parser.add_argument('--sample-prevalence', default=None, type=float,
	help='If running a case-control study simulation, the prevalence of the cases in the study sample [Default: None]. '
	'The \'None\' default keeps the study prevalence the same as the population prevalence.')
parser.add_argument('--ldscore-sampling-prop', default=None, type=float,
	help='If running a large ascertained case-control simulation, you may want to determine LD score estimates a '
	'subset of the individuals in the tree to increase speed.')
parser.add_argument('--ldscore-within-sample', default=False, action='store_true',
	help='Do you want to evaluate the LD scores using the case-control sample used to obtain the effect size estimates? '
	'Note that this will result in LD scores being generated for each simulation, so may slow things down if ascertainment is '
	'low and the sample size is large.')
parser.add_argument('--ldsc', default=False, action='store_true',
	help='Do we perform LD score regression?')
parser.add_argument('--include-pop-strat', default=False, action='store_true',
	help='Do we include population stratification in the contribution to the phenotype. As default, we randomly draw the mean from '
	'a Normal with mean 0 and variance 1')
parser.add_argument('--s2', default=0.1, type=float,
	help='What is the clade associated variance in the phenotype?')
parser.add_argument('--no-migration', default=False, action='store_true',
	help='Turn off migration in the demographic history - currently only has an effect for \'out_of_africa\' in the sim-type flag')
parser.add_argument('--vcf', default=False, action='store_true', 
	help='If saving the trees, do we want to save as .vcf file? Default is PLINK .bed/.bim/.fam format if this flag is not used.')
parser.add_argument('--write-pheno', default=False, action='store_true', 
	help='Do you want to write the phenotypes to disk?')
parser.add_argument('--prop-EUR', default=0.75, type=float,
	help='What proportion of samples does the European portion make up?')
parser.add_argument('--write-l2', default=False, action='store_true', 
	help='Do you want to write the LD scores for this simulation to disk?')
parser.add_argument('--debug', default=False, action='store_true', 
	help='Use seed for simulation to ensure the same output for debugging purposes?')
parser.add_argument('--load-tree-sequence', default=None, type=str,
	help='Do you want to read in a tree sequence to simulate phenotypes down? Pass the file-path here to a dumped tree sequence.')
parser.add_argument('--dump-trees', default=False, action='store_true',
	help='Do you want to dump the simulated tree sequences to disk in their native format?')

if __name__ == '__main__':

	args = parser.parse_args()
	log = pr.Logger(args.out+'.msprime.log')
	try:
		defaults = vars(parser.parse_args(''))
		opts = vars(args)
		non_defaults = [x for x in opts.keys() if opts[x] != defaults[x]]
		header = ''
		header += 'Call: \n'
		header += './msprimesim.py \\\n'
		options = ['--'+x.replace('_','-')+' '+str(opts[x])+' \\' for x in non_defaults]
		header += '\n'.join(options).replace('True','').replace('False','')
		header = header[0:-1]+'\n'
		log.log(header)
		log.log('Beginning analysis at {T}'.format(T=time.ctime()))
		start_time = time.time()
		out_fname = args.out + '.h2'

		if args.pcgc:
			h2_ldsc, h2_pcgc, h2_ldsc_int = simulate_tree_and_betas(args, log)
			out_fname_pcgc = args.out + '.pcgc'
			
			df = pd.DataFrame.from_records(np.c_[h2_pcgc['h2_A'], h2_pcgc['h2_D'], h2_pcgc['h2_AC']])
			df.columns = ['h2_A', 'h2_D', 'h2_AC']
			df.to_csv(out_fname_pcgc, sep='\t', header=True, index=False, float_format='%.3f')
		else:
			h2_ldsc, h2_pcgc, h2_ldsc_int = simulate_tree_and_betas(args, log)

		df = pd.DataFrame.from_records(np.c_[
			h2_ldsc['h2_A'], h2_ldsc['int_A'],
			h2_ldsc['h2_D'], h2_ldsc['int_D'],
			h2_ldsc['h2_AC'], h2_ldsc['int_AC']])
		df.columns = ['h2_A', 'int_A', 'h2_D', 'int_D', 'h2_AC', 'int_AC']
		df.to_csv(out_fname, sep='\t', header=True, index=False, float_format='%.3f')

		if args.free_and_no_intercept:
			out_fname = args.out + '.int.h2'

			df = pd.DataFrame.from_records(np.c_[
			h2_ldsc_int['h2_A'], h2_ldsc_int['int_A'],
			h2_ldsc_int['h2_D'], h2_ldsc_int['int_D'],
			h2_ldsc_int['h2_AC'], h2_ldsc_int['int_AC']])
			df.columns = ['h2_A', 'int_A', 'h2_D', 'int_D', 'h2_AC', 'int_AC']
			df.to_csv(out_fname, sep='\t', header=True, index=False, float_format='%.3f')

	except Exception:
		ex_type, ex, tb = sys.exc_info()
		log.log( traceback.format_exc(ex) )
		raise
	finally:
		log.log('Analysis finished at {T}'.format(T=time.ctime()) )
		time_elapsed = round(time.time()-start_time,2)
		log.log('Total time elapsed: {T}'.format(T=pr.sec_to_str(time_elapsed)))

