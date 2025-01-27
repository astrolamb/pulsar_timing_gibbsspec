from __future__ import division

import os, time, sys, shutil

import numpy as np
import scipy.linalg as sl
import acor
from tqdm import tqdm

from enterprise.signals import selections
from PTMCMCSampler.PTMCMCSampler import PTSampler as ptmcmc


class PulsarBlockGibbs(object):
    
    """Gibbs-based pulsar-timing periodogram analysis.
    
    Based on:
    
        Article by van Haasteren & Vallisneri (2014),
        "New advances in the Gaussian-process approach 
        to pulsar-timing data analysis",
        Physical Review D, Volume 90, Issue 10, id.104012
        arXiv:1407.1838
        
        Code based on https://github.com/jellis18/gibbs_student_t
        
    Authors: 
    
        S. R. Taylor
    
    Example usage:
    
        > gibbs = PTABlockGibbs(pta, hypersample='conditional', ecorrsample='mh', psr=psr)
        > x0 = x0 = np.concatenate([p.sample().flatten() for p in gibbs.params])
        > gibbs.sample(x0, outdir='./', 
                       niter=10000, resume=False)
        
     
    """
    
    def __init__(self, pta, hypersample='conditional', 
                 ecorrsample='mh', psr=None):
        """
        Parameters
        -----------
        pta : object
            instance of a pta object for a single pulsar
        hypersample: string
            method to draw free spectral coefficients from conditional posterior
            ('conditional' = analytic; 'mh' = short MCMC chain)
        ecorrsample: string
            method to draw ECORR coefficients from conditional posterior
            ('conditional' = analytic; 'mh' = short MCMC chain)
        psr: enterprise pulsar object
            pass an enterprise pulsar object to get ecorr selections
        """

        self.pta = pta
        self.pulsar_name = pta.pulsars[0]
        self.hypersample = hypersample
        self.ecorrsample = ecorrsample

        # checking if kernel ecorr is being used
        signal_names = [pta.signals[sc].__class__.__name__
                        for sc in pta.signals]  # list of signal classes
        if np.any(['EcorrKernelNoise' in sc for sc in signal_names]):
            raise TypeError('Gibbs outlier analysis must use basis_ecorr, not kernel ecorr')

        # For now assume one pulsar
        self._residuals = self.pta.get_residuals()[0]

        # auxiliary variable stuff
        xs = [p.sample() for p in pta.params]
        self._b = np.zeros(self.pta.get_basis(xs)[0].shape[1])

        # for caching
        self.TNT = None
        self.d = None
        
        # grabbing priors on free spectral power values
        for ct, par in enumerate([p.name for p in self.params]):
            if 'rho' in par and 'gw' in par: ind = ct
        rho_priors = str(self.params[ind].params[0])
        rho_priors = rho_priors.split('(')[1].split(')')[0].split(', ')
        self.rhomin, self.rhomax = (10**(2*float(rho_priors[0].split('=')[1])), 
                                    10**(2*float(rho_priors[1].split('=')[1])))
        
        # find basis indices of GW and ECORR processes
        ct = 0
        self.b_param_names = []
        self.gwid = None
        self.ecid = None
        for sig in self.pta.signals:
            Fmat = self.pta.signals[sig].get_basis()
            if 'gw' in self.pta.signals[sig].name:
                self.gwid = ct + np.arange(0,Fmat.shape[1])
            if 'ecorr' in self.pta.signals[sig].name:
                self.ecid = ct + np.arange(0,Fmat.shape[1])
            # Avoid None-basis processes.
            # Also assume red + GW signals share basis.
            if Fmat is not None and 'red' not in sig:
                ct += Fmat.shape[1]
                self.b_param_names += [sig+'_'+str(ii) 
                                      for ii in range(Fmat.shape[1])]
        if ct == self.pta.get_basis()[0].shape[1]:
            print('Basis count is good')
        else:
            print('WARNING: Miscounted basis entries. Maybe red noise and GW do not share a design matrix.')

        if self.ecid is not None:   
            # grabbing priors on ECORR params
            for ct, par in enumerate([p.name for p in self.params]):
                if 'ecorr' in par: ind = ct
            ecorr_priors = str(self.params[ind].params[0])
            ecorr_priors = ecorr_priors.split('(')[1].split(')')[0].split(', ')
            self.ecorrmin, self.ecorrmax = (10**(2*float(ecorr_priors[0].split('=')[1])), 
                                            10**(2*float(ecorr_priors[1].split('=')[1])))

            # find ECORR epochs covered by each selection
            if self.ecorrsample == 'conditional':
                self.Umat = self.pta.get_basis()[0][:,self.ecid]
                self.sel = selections.by_backend(psr.flags['f'])
                self.ecorr_inds_sel = []
                for key in self.sel:
                    inds_sel_tmp = self.Umat[self.sel[key],:]
                    self.ecorr_inds_sel.append(np.where(np.sum(inds_sel_tmp,axis=0)>0.)[0])

        # identify intrinsic red noise and gw signals
        self.red_sig = None
        self.gw_sig = None
        for sig in self.pta.signals:
            if 'red' in self.pta.signals[sig].name:
                self.red_sig = self.pta.signals[sig]
            if 'gw' in self.pta.signals[sig].name:
                self.gw_sig = self.pta.signals[sig]

                       
    @property
    def params(self):
        ret = []
        for param in self.pta.params:
            ret.append(param)
        return ret
    
    @property
    def param_names(self):
        ret = []
        for p in self.params:
            if p.size:
                for ii in range(0, p.size):
                    ret.append(p.name + "_{}".format(ii))
            else:
                ret.append(p.name)
        return ret
    
    def map_params(self, xs):
        ret = {}
        ct = 0
        for p in self.params:
            n = p.size if p.size else 1
            ret[p.name] = xs[ct : ct + n] if n > 1 else float(xs[ct])
            ct += n
        return ret


    def get_gwrho_param_indices(self):
        ind = []
        for ct, par in enumerate(self.param_names):
            if 'rho' in par:
                ind.append(ct)
        return np.array(ind)


    def get_red_param_indices(self):
        ind = []
        for ct, par in enumerate(self.param_names):
            if 'log10_A' in par or 'gamma' in par:
                ind.append(ct)
        return np.array(ind)


    def get_efacequad_indices(self):
        ind = []
        for ct, par in enumerate(self.param_names):
            if 'efac' in par or 'equad' in par:
                ind.append(ct)
        return np.array(ind)
    

    def get_ecorr_indices(self):
        ind = []
        for ct, par in enumerate(self.param_names):
            if 'ecorr' in par:
                ind.append(ct)
        return np.array(ind)


    def update_gwrho_params(self, xs):

        # get hyper parameter indices
        gwind = self.get_gwrho_param_indices()

        xnew = xs.copy()

        if self.hypersample == 'conditional':

            tau = self._b[self.gwid]**2
            tau = (tau[::2] + tau[1::2]) / 2

            if self.red_sig is None:
                # draw variance values analytically,
                # a la van Haasteren & Vallisneri (2014)

                eta = np.random.uniform(0, 1-np.exp((tau/self.rhomax) - (tau/self.rhomin)))
                rhonew = tau / ((tau/self.rhomax) - np.log(1-eta))

            else:
                # draw variance values from numerical CDF,
                # based on van Haasteren & Vallisneri (2014)

                if self.red_sig is not None:
                    irn = np.array(self.red_sig.get_phi(self.map_params(xnew)))[::2]
                else:
                    irn = np.zeros(tau.shape[0])
                
                # grid is uniform in log10(rho); implicit log-uniform prior
                rho_tmp = 10**np.linspace(np.log10(self.rhomin), np.log10(self.rhomax), 1000)
                logratio = np.log(tau[:,None]) - np.logaddexp.outer(np.log(irn), np.log(rho_tmp))
                logpdf = logratio - np.exp(logratio) #+ np.log(10) # correct for log10 to log
                # use Gumbel Max Trick 
                # https://homes.cs.washington.edu/~ewein//blog/2022/03/04/gumbel-max/
                gumbels = np.random.gumbel(size=(tau.shape[0],rho_tmp.shape[0]))
                rhonew = rho_tmp[np.argmax(logpdf + gumbels, axis=1)]

            xnew[gwind] = 0.5*np.log10(rhonew)
          
        else:
            print('ERROR: Only conditional draws on rho for now...')
            '''
            # get initial log-likelihood and log-prior
            lnlike0, lnprior0 = self.get_lnlikelihood(xs), self.get_lnprior(xs)
            
            for ii in range(10):

                # standard gaussian jump (this allows for different step sizes)
                q = xnew.copy()
                sigmas = 0.05 * len(gwind)
                probs = [0.1, 0.15, 0.5, 0.15, 0.1]
                sizes = [0.1, 0.5, 1.0, 3.0, 10.0]
                scale = np.random.choice(sizes, p=probs)
                par = np.random.choice(gwind, size=1) # only one hyper param at a time
                q[par] += np.random.randn(len(q[par])) * sigmas * scale

                # get log-like and log prior at new position
                lnlike1, lnprior1 = self.get_lnlikelihood(q), self.get_lnprior(q)

                # metropolis step
                diff = (lnlike1 + lnprior1) - (lnlike0 + lnprior0)
                if diff > np.log(np.random.rand()):
                    xnew = q
                    lnlike0 = lnlike1
                    lnprior0 = lnprior1
                else:
                    xnew = xnew
            '''
        
        return xnew


    def update_red_params(self, xs, iters=None):

        # get hyper parameter indices
        rind = self.get_red_param_indices()

        xnew = xs.copy()

        # this block should run at the start of sampling to estimate the posterior covariance matrix
        if iters is not None:

            # get initial log-likelihood and log-prior
            # use the full marginalized PTA likelihood to sample everything
            lnlike0 = self.get_lnlikelihood_fullmarg(xnew)
            lnprob0 = self.get_lnlikelihood_fullmarg(xnew) + self.get_lnprior(xnew)

            # setup ptmcmc sampler
            outDir = f'./dummy_{self.pulsar_name}/'
            self.ptsampler_rn = ptmcmc(ndim=len(xnew), 
                                       logl=self.get_lnlikelihood_fullmarg, 
                                       logp=self.get_lnprior,
                                       cov=0.01*np.diag(np.ones_like(xnew)), 
                                       groups=None, verbose=False, 
                                       resume=False, outDir=outDir)
            # sample everything to estimate cov matrix and de buffer
            self.ptsampler_rn.sample(xnew, iters, SCAMweight=30, AMweight=15, 
                                     DEweight=50, isave=iters+1, burn=iters-1)
            xnew, _, _ = self.ptsampler_rn.PTMCMCOneStep(xnew, lnlike0, lnprob0, 0)
            
            # select only red noise as sampling group from now on
            self.ptsampler_rn.groups = [rind]
            # grab only red noise portions of cov matrix
            self.ptsampler_rn.cov = self.ptsampler_rn.cov[rind,:][:,rind]
            
            # update parameter group svd for jumps
            self.ptsampler_rn.U = [[]] * len(self.ptsampler_rn.groups)
            self.ptsampler_rn.S = [[]] * len(self.ptsampler_rn.groups)
            self.ptsampler_rn.U[0], self.ptsampler_rn.S[0], _ = \
                np.linalg.svd(self.ptsampler_rn.cov)

            # update likelihood to be red-noise only
            self.ptsampler_rn.logl = self.get_lnlikelihood_red
            self.ptsampler_rn.logp = self.get_lnprior
            
            # delete the dummy directory that ptmcmcsampler makes
            shutil.rmtree(outDir)

        elif iters is None:

            # get initial log-likelihood and log-prior
            # use the red-noise portion of the unmarginalized likelihood
            lnlike0 = self.get_lnlikelihood_red(xnew)
            lnprob0 = self.get_lnlikelihood_red(xnew) + self.get_lnprior(xnew)

            # run the ptmcmc sampler for 20 steps
            step_max = 20
            for _ in range(step_max):
                xnew, _, _ = self.ptsampler_rn.PTMCMCOneStep(xnew, lnlike0, lnprob0, 0)
        
        return xnew


    def update_white_params(self, xs, iters=None):

        # get white noise parameter indices
        wind = self.get_efacequad_indices()

        xnew = xs.copy()
        lnlike0, lnprior0 = self.get_lnlikelihood_white(xnew), self.get_lnprior(xnew)
        
        if iters is not None:
            
            short_chain = np.zeros((iters,len(wind)))
            for ii in range(iters):
                # standard gaussian jump (this allows for different step sizes)
                q = xnew.copy()
                sigmas = 0.05 * len(wind)
                probs = [0.1, 0.15, 0.5, 0.15, 0.1]
                sizes = [0.1, 0.5, 1.0, 3.0, 10.0]
                scale = np.random.choice(sizes, p=probs)
                par = np.random.choice(wind, size=1)
                q[par] += np.random.randn(len(q[par])) * sigmas * scale

                # get log-like and log prior at new position
                lnlike1, lnprior1 = self.get_lnlikelihood_white(q), self.get_lnprior(q)

                # metropolis step
                diff = (lnlike1 + lnprior1) - (lnlike0 + lnprior0)
                if diff > np.log(np.random.rand()):
                    xnew = q
                    lnlike0 = lnlike1
                    lnprior0 = lnprior1
                else:
                    xnew = xnew
                    
                short_chain[ii,:] = q[wind]
                
            self.cov_white = np.cov(short_chain[100:,:],rowvar=False)
            self.sigma_white = np.diag(self.cov_white)**0.5
            self.svd_white = np.linalg.svd(self.cov_white)
            self.aclength_white = int(np.max([int(acor.acor(short_chain[100:,jj])[0]) 
                                                  for jj in range(len(wind))]))
            
        elif iters is None:
            
            for ii in range(self.aclength_white):
                # standard gaussian jump (this allows for different step sizes)
                q = xnew.copy()
                sigmas = 0.05 * len(wind)
                probs = [0.1, 0.15, 0.5, 0.15, 0.1]
                sizes = [0.1, 0.5, 1.0, 3.0, 10.0]
                scale = np.random.choice(sizes, p=probs)
                par = np.random.choice(wind, size=1)
                q[par] += np.random.randn(len(q[par])) * sigmas * scale
                
                #q[wind] += np.random.multivariate_normal(np.zeros(len(wind)),
                #                                         2.38**2 * self.cov_white / len(wind))
                #q[wind] += (2.38/ len(wind)) * np.dot(np.random.randn(len(wind)), 
                #                                      np.sqrt(self.svd_white[1])[:, None] * 
                #                                      self.svd_white[2])
                #par = np.random.choice(wind, size=1)
                #sigmas = self.sigma_white[list(wind).index(par)]
                #q[par] += 2.38 * np.random.randn(len(q[par])) * sigmas

                # get log-like and log prior at new position
                lnlike1, lnprior1 = self.get_lnlikelihood_white(q), self.get_lnprior(q)

                # metropolis step
                diff = (lnlike1 + lnprior1) - (lnlike0 + lnprior0)
                if diff > np.log(np.random.rand()):
                    xnew = q
                    lnlike0 = lnlike1
                    lnprior0 = lnprior1
                else:
                    xnew = xnew
             
        return xnew
    
    
    def update_ecorr_params(self, xs, iters=None):

        # NEEDS TO BE FIXED...

        # get ecorr parameter indices
        eind = self.get_ecorr_indices()

        xnew = xs.copy()
        
        lnlike0, lnprior0 = self.get_lnlikelihood(xnew), self.get_lnprior(xnew)
        
        if iters is not None:
            
            short_chain = np.zeros((iters,len(eind)))
            for ii in range(iters):

                # standard gaussian jump (this allows for different step sizes)
                q = xnew.copy()
                sigmas = 0.05 * len(eind)
                probs = [0.1, 0.15, 0.5, 0.15, 0.1]
                sizes = [0.1, 0.5, 1.0, 3.0, 10.0]
                scale = np.random.choice(sizes, p=probs)
                par = np.random.choice(eind, size=1)
                q[par] += np.random.randn(len(q[par])) * sigmas * scale

                # get log-like and log prior at new position
                lnlike1, lnprior1 = self.get_lnlikelihood(q), self.get_lnprior(q)

                # metropolis step
                diff = (lnlike1 + lnprior1) - (lnlike0 + lnprior0)
                if diff > np.log(np.random.rand()):
                    xnew = q
                    lnlike0 = lnlike1
                    lnprior0 = lnprior1
                else:
                    xnew = xnew
                    
                short_chain[ii,:] = q[eind]
                
            self.cov_ecorr = np.cov(short_chain[100:,:],rowvar=False)
            self.sigma_ecorr = np.diag(self.cov_ecorr)**0.5
            self.svd_ecorr = np.linalg.svd(self.cov_ecorr)
            self.aclength_ecorr = int(np.max([int(acor.acor(short_chain[100:,jj])[0]) 
                                                  for jj in range(len(eind))]))
            
        elif iters is None:
            
            for ii in range(self.aclength_ecorr):
                q = xnew.copy()
                sigmas = 0.05 * len(eind)
                probs = [0.1, 0.15, 0.5, 0.15, 0.1]
                sizes = [0.1, 0.5, 1.0, 3.0, 10.0]
                scale = np.random.choice(sizes, p=probs)
                par = np.random.choice(eind, size=1)
                q[par] += np.random.randn(len(q[par])) * sigmas * scale
                
                #q[eind] += np.random.multivariate_normal(np.zeros(len(eind)),
                #                                         2.38**2 * self.cov_ecorr / len(eind))
                #q[eind] += (2.38/ len(eind)) * np.dot(np.random.randn(len(eind)), 
                #                                      np.sqrt(self.svd_ecorr[1])[:, None] * 
                #                                      self.svd_ecorr[2])
                #par = np.random.choice(eind, size=1)
                #sigmas = self.sigma_ecorr[list(eind).index(par)]
                #q[par] += 2.38 * np.random.randn(len(q[par])) * sigmas

                # get log-like and log prior at new position
                lnlike1, lnprior1 = self.get_lnlikelihood(q), self.get_lnprior(q)

                # metropolis step
                diff = (lnlike1 + lnprior1) - (lnlike0 + lnprior0)
                if diff > np.log(np.random.rand()):
                    xnew = q
                    lnlike0 = lnlike1
                    lnprior0 = lnprior1
                else:
                    xnew = xnew
                    
        return xnew

    
    def update_b(self, xs): 

        # map parameter vector
        params = self.map_params(xs)

        # get auxiliaries
        Nvec = self.pta.get_ndiag(params)[0]
        phiinv = self.pta.get_phiinv(params, logdet=False)[0]
        residuals = self._residuals

        T = self.pta.get_basis(params)[0]
        if self.TNT is None and self.d is None:
            self.TNT = np.dot(T.T, T / Nvec[:,None])
            self.d = np.dot(T.T, residuals/Nvec)

        # Red noise piece
        Sigma = self.TNT + np.diag(phiinv)

        try:
            u, s, _ = sl.svd(Sigma)
            mn = np.dot(u, np.dot(u.T, self.d)/s)
            Li = u * np.sqrt(1/s)
        except np.linalg.LinAlgError:
            Q, R = sl.qr(Sigma)
            Sigi = sl.solve(R, Q.T)
            mn = np.dot(Sigi, self.d)
            u, s, _ = sl.svd(Sigi)
            Li = u * np.sqrt(1/s)

        b = mn + np.dot(Li, np.random.randn(Li.shape[0]))

        return b

    
    def get_lnlikelihood_white(self, xs):

        # map parameters
        params = self.map_params(xs)
        matrix = self.pta.get_ndiag(params)[0]
        
        # Nvec and Tmat
        Nvec = matrix
        Tmat = self.pta.get_basis(params)[0]

        # whitened residuals
        mn = np.dot(Tmat, self._b)
        yred = self._residuals - mn

        # log determinant of N
        logdet_N = np.sum(np.log(Nvec))

        # triple product in likelihood function
        rNr = np.sum(yred**2/Nvec)

        # first component of likelihood function
        loglike = -0.5 * (logdet_N + rNr)

        return loglike


    def get_lnlikelihood_red(self, xs):

        # map parameters
        params = self.map_params(xs)

        # adding together squares of GP coefficients
        tau = self._b[self.gwid]**2
        tau = (tau[::2] + tau[1::2]) / 2

        # get intrinsic red noise and gw psd values
        irn = np.array(self.red_sig.get_phi(params))[::2]
        gwsig = np.array(self.gw_sig.get_phi(params))[::2]
        
        # compute the log-likelihood
        logratio = np.log(tau) - np.logaddexp(np.log(irn), np.log(gwsig))
        loglike = np.sum(logratio - np.exp(logratio))

        return loglike


    def get_lnlikelihood_fullmarg(self, xs):

        # map parameter vector
        params = self.map_params(xs)
        
        # start likelihood calculations
        loglike = 0

        # get auxiliaries
        Nvec = self.pta.get_ndiag(params)[0]
        phiinv, logdet_phi = self.pta.get_phiinv(params, 
                                                 logdet=True)[0]
        residuals = self._residuals

        T = self.pta.get_basis(params)[0]
        if self.TNT is None and self.d is None:
            self.TNT = np.dot(T.T, T / Nvec[:,None])
            self.d = np.dot(T.T, residuals/Nvec)

        # log determinant of N
        logdet_N = np.sum(np.log(Nvec))

        # triple product in likelihood function
        rNr = np.sum(residuals**2/Nvec)

        # first component of likelihood function
        loglike += -0.5 * (logdet_N + rNr)

        # Red noise piece
        Sigma = self.TNT + np.diag(phiinv)

        try:
            cf = sl.cho_factor(Sigma)
            expval = sl.cho_solve(cf, self.d)
        except np.linalg.LinAlgError:
            return -np.inf

        logdet_sigma = np.sum(2 * np.log(np.diag(cf[0])))
        loglike += 0.5 * (np.dot(self.d, expval) - 
                          logdet_sigma - logdet_phi)

        return loglike

        
    def get_lnprior(self, params):
        # map parameter vector if needed
        params = params if isinstance(params, dict) else self.map_params(params)

        return np.sum([p.get_logpdf(params=params) for p in self.params])


    def sample(self, xs, outdir='./', niter=10000, resume=False):

        print(f'Creating chain directory: {outdir}')
        os.system(f'mkdir -p {outdir}')

        np.savetxt(f'{outdir}/pars_chain.txt', self.param_names, fmt="%s")
        np.savetxt(f'{outdir}/pars_bchain.txt', self.b_param_names, fmt="%s")
        
        self.chain = np.zeros((niter, len(xs)))
        self.bchain = np.zeros((niter, len(self._b)))
        
        self.iter = 0
        startLength = 0
        xnew = xs
        if resume:
            print('Resuming from previous run...')
            # read in previous chains
            tmp_chains = []
            tmp_chains.append(np.loadtxt(f'{outdir}/chain.txt'))
            tmp_chains.append(np.loadtxt(f'{outdir}/bchain.txt'))
            
            # find minimum length
            minLength = np.min([tmp.shape[0] for tmp in tmp_chains])
            
            # take only the minimum length entries of each chain
            tmp_chains = [tmp[:minLength] for tmp in tmp_chains]
            
            # pad with zeros if shorter than niter
            self.chain[:tmp_chains[0].shape[0]] = tmp_chains[0]
            self.bchain[:tmp_chains[1].shape[0]] = tmp_chains[1]
            
            # set new starting point for sampling
            startLength = minLength
            xnew = self.chain[startLength-1]
            
        #tstart = time.time()
        for ii in tqdm(range(startLength, niter)):
            self.iter = ii
            self.chain[ii, :] = xnew
            self.bchain[ii,:] = self._b
            
            if ii==0:
                self._b = self.update_b(xs)

            self.TNT = None
            self.d = None

            # update efac/equad parameters
            if self.get_efacequad_indices().size != 0:
                if ii==0:
                    xnew = self.update_white_params(xnew, iters=1000)
                else:
                    xnew = self.update_white_params(xnew, iters=None)
            

            # update ecorr parameters
            if self.get_ecorr_indices().size != 0:
                print('ERROR: No ECORR for now...')
                '''
                if ii==0:
                    xnew = self.update_ecorr_params(xnew, iters=1000)
                else:
                    xnew = self.update_ecorr_params(xnew, iters=None)
                '''

            # update red noise parameters
            if self.get_red_param_indices().size != 0:
                if ii==0:
                    xnew = self.update_red_params(xnew, iters=10000)
                else:
                    xnew = self.update_red_params(xnew, iters=None)

            # update gw free-spectral hyper-parameters
            if self.get_gwrho_param_indices().size != 0:
                xnew = self.update_gwrho_params(xnew)

            # if accepted, update quadratic params
            if np.all(xnew != self.chain[ii,-1]):
                self._b = self.update_b(xnew)


            if ii % 100 == 0 and ii > 0:
                #sys.stdout.write('\r')
                #sys.stdout.write('Finished %g percent in %g seconds.'%(ii / niter * 100, 
                #                                                       time.time()-tstart))
                #sys.stdout.flush()

                # TO DO: these really should be hdf5 files. Make structured
                # Also add functionality to read with la-forge
                np.save(f'{outdir}/chain.npy', self.chain[:ii+1, :])
                np.save(f'{outdir}/bchain.npy', self.bchain[:ii+1, :])