#!/usr/bin/env python
"""
This file introduces an extension to the basic Study-class that allows to compute the distribution of hyper-parameters.
"""

from .study import *
from .preprocessing import *
from scipy.misc import logsumexp
from scipy.misc import factorial
from mpl_toolkits.mplot3d import Axes3D
from copy import copy
import sympy.abc as abc
from sympy import lambdify
from sympy.stats import density


class RasterStudy(Study):
    """
    This class serves as an extention to the basic Study class and allows to compute the distribution of hyper-
    parameters of a given transition model. For further information, see the documentation of the fit-method of this
    class.
    """
    def __init__(self):
        super(RasterStudy, self).__init__()

        self.raster = []
        self.rasterValues = []
        self.rasterConstant = []
        self.hyperParameterPrior = None
        self.hyperParameterDistribution = None
        self.averagePosteriorSequence = None
        self.logEvidenceList = []
        self.localEvidenceList = []

        print '  --> Raster study'

    def fit(self, raster=[], prior=[], forwardOnly=False, evidenceOnly=False, customRaster=False, silent=False, nJobs=1):
        """
        This method over-rides the according method of the Study-class. It runs the algorithm for equally spaced hyper-
        parameter values as defined by the variable 'raster'. The posterior sequence represents the average
        model of all analyses. Posterior mean values are computed from this average model.

        Parameters:
            raster - List of lists with each containing the name of a hyper-parameter together with a lower and upper
                boundary as well as a number of steps in between.
                Example: raster = [['sigma', 0, 1, 20],['log10pMin', -10, -5, 10]]

            prior - List of SymPy random variables, each of which represents the prior distribution of one hyper-
                parameter. The multiplicative probability (density) will be assigned to the individual raster points.
                The resulting prior distribution is renormalized such that the sum over all points specified by the
                raster equals one.

            forwardOnly - If set to True, the fitting process is terminated after the forward pass. The resulting
                posterior distributions are so-called "filtering distributions" which - at each time step -
                only incorporate the information of past data points. This option thus emulates an online
                analysis.

            evidenceOnly - If set to True, only forward pass is run and evidence is calculated. In contrast to the
                forwardOnly option, no posterior mean values are computed and no posterior distributions are stored.

            customRaster - If set to True, the keyword argument 'raster' will not be used. Instead, all relevant
                attributes have to be set manually by the user. May be used for irregular grids of hyper-parameter
                values.

            silent - If set to True, no output is generated by the fitting method.

            nJobs - Number of processes to employ. Multiprocessing is based on the 'pathos' module.

        Returns:
            None
        """
        print '+ Started new fit.'

        if not customRaster:
            self.raster = raster

            # in case no raster is provided, call standard fit method.
            if not self.raster:
                print '! No raster defined for hyper-parameter values. Using standard fit-method.'
                Study.fit(self, forwardOnly=forwardOnly, evidenceOnly=evidenceOnly, silent=silent)
                return

            # create array with raster-values
            temp = np.meshgrid(*[np.linspace(lower, upper, steps) for name, lower, upper, steps in self.raster],
                               indexing='ij')
            self.rasterValues = np.array([t.ravel() for t in temp]).T
            self.rasterConstant = [np.abs(upper-lower)/(float(steps)-1) for name, lower, upper, steps in self.raster]
        else:
            if self.raster == []:
                print "! A dummy 'raster' attribute has to be set when using customRaster=True."
                print "  (Only hyper-parameter names are extracted from this attribute.)"
                return
            if self.rasterValues == []:
                print "! The attribute 'rasterValues' has to be set manually when using customRaster=True."
                return
            if self.rasterConstant == []:
                print "! The attribute 'rasterConstant' has to be set manually when using customRaster=True."
                return

        self.formattedData = movingWindow(self.rawData, self.observationModel.segmentLength)

        # determine prior distribution
        if not prior:
            self.hyperParameterPrior = np.ones(len(self.rasterValues))/len(self.rasterValues)
        else:
            # check if given prior is correctly formatted to fit length of raster array.
            # we use 'len(self.rasterValues[0])' because self.raster is reformatted within changepointStudy, when using
            # break-points.
            if len(prior) != len(self.rasterValues[0]):
                print '! {} hyper-parameters are specified in raster. Priors are provided for {}.'\
                    .format(len(self.rasterValues[0]), len(prior))
                return
            else:
                densities = []
                self.hyperParameterPrior = np.ones(len(self.rasterValues))
                for i, rv in enumerate(prior):  # loop over all specified priors
                    if len(list(rv._sorted_args[0].distribution.free_symbols)) > 0:
                        print '! Prior distribution must not contain free parameters.'
                        return

                    # get symbolic representation of probability density
                    x = abc.x
                    symDensity = density(rv)(x)

                    # get density as lambda function
                    pdf = lambdify([x], symDensity, modules=['numpy', {'factorial': factorial}])

                    # update hyper-parameter prior
                    self.hyperParameterPrior *= pdf(self.rasterValues[:, i])

                # renormalize hyper-parameter prior
                self.hyperParameterPrior /= np.sum(self.hyperParameterPrior)

        if not evidenceOnly:
            self.averagePosteriorSequence = np.zeros([len(self.formattedData)]+self.gridSize)

        self.logEvidenceList = []
        self.localEvidenceList = []

        # we use the setSelectedHyperParameters-method from the Study class
        self.selectedHyperParameters = [name for name, lower, upper, steps in self.raster]

        if not self.checkConsistency():
            return

        print '    + {} analyses to run.'.format(len(self.rasterValues))

        # check if multiprocessing is available
        if nJobs > 1:
            try:
                from pathos.multiprocessing import ProcessPool
            except:
                print "! Install 'pathos.multiprocessing' to enable multiprocessing."
                print "! Switching back to single process."
                nJobs=1

        # prepare parallel execution if necessary
        if nJobs > 1:
            print '    + Creating {} processes.'.format(nJobs)
            pool = ProcessPool(nodes=nJobs)

            # use parallelFit method to create copies of this RasterStudy instance with only partial raster values
            subStudies = pool.map(self.parallelFit,
                                  range(nJobs),
                                  [nJobs]*nJobs,
                                  [forwardOnly]*nJobs,
                                  [evidenceOnly]*nJobs,
                                  [silent]*nJobs)

            # merge all sub-studies
            for S in subStudies:
                self.logEvidenceList += S.logEvidenceList
                self.localEvidenceList += S.localEvidenceList
                if not evidenceOnly:
                    self.averagePosteriorSequence += S.averagePosteriorSequence
        # single process fit
        else:
            for i, hyperParamValues in enumerate(self.rasterValues):
                self.setSelectedHyperParameters(hyperParamValues)

                # call fit method from parent class
                Study.fit(self, forwardOnly=forwardOnly, evidenceOnly=evidenceOnly, silent=True)

                self.logEvidenceList.append(self.logEvidence)
                self.localEvidenceList.append(self.localEvidence)
                if not evidenceOnly:
                    self.averagePosteriorSequence += self.posteriorSequence *\
                                                     np.exp(self.logEvidence - self.logEvidenceList[0]) *\
                                                     self.hyperParameterPrior[i]

                if not silent:
                    print '    + Analysis #{} of {} -- Hyper-parameter values {} -- log10-evidence = {:.5f}'\
                        .format(i+1, len(self.rasterValues), hyperParamValues, self.logEvidence / np.log(10))

        # reset list of parameters to optimize, so that unpacking and setting hyper-parameters works as expected
        self.selectedHyperParameters = []

        if not evidenceOnly:
            # compute average posterior distribution
            normalization = np.array([np.sum(posterior) for posterior in self.averagePosteriorSequence])
            for i in range(len(self.grid)):
                normalization = normalization[:, None]  # add axis; needs to match averagePosteriorSequence
            self.averagePosteriorSequence /= normalization

            # set self.posteriorSequence to average posterior sequence for plotting reasons
            self.posteriorSequence = self.averagePosteriorSequence

            if not silent:
                print '    + Computed average posterior sequence'

        # compute log-evidence of average model
        self.logEvidence = logsumexp(np.array(self.logEvidenceList) + np.log(self.hyperParameterPrior))

        print '    + Log10-evidence of average model: {:.5f}'.format(self.logEvidence / np.log(10))

        # compute hyper-parameter distribution
        logHyperParameterDistribution = self.logEvidenceList + np.log(self.hyperParameterPrior)
        scaledLogHyperParameterDistribution = logHyperParameterDistribution - np.mean(logHyperParameterDistribution)
        self.hyperParameterDistribution = np.exp(scaledLogHyperParameterDistribution)
        self.hyperParameterDistribution /= np.sum(self.hyperParameterDistribution)
        self.hyperParameterDistribution /= np.prod(self.rasterConstant)  # probability density

        if not silent:
            print '    + Computed hyper-parameter distribution'

        # compute local evidence of average model
        self.localEvidence = np.sum((np.array(self.localEvidenceList).T*self.hyperParameterDistribution).T, axis=0)

        if not silent:
            print '    + Computed local evidence of average model'

        # compute posterior mean values
        if not evidenceOnly:
            self.posteriorMeanValues = np.empty([len(self.grid), len(self.posteriorSequence)])
            for i in range(len(self.grid)):
                self.posteriorMeanValues[i] = np.array([np.sum(p*self.grid[i]) for p in self.posteriorSequence])

            if not silent:
                print '    + Computed mean parameter values.'

        # clear self.hyperParameterPrior (in case fit is called after changing self.raster)
        self.hyperParameterPrior = None

        # discard evidence values of individual fits
        self.logEvidenceList = []
        self.localEvidenceList = []

        print '+ Finished fit.'

    def parallelFit(self, idx, nJobs, forwardOnly, evidenceOnly, silent):
        """
        This method is called by the fit method of the RasterStudy class. It creates a copy of the current class
        instance and performs a fit based on a subset of the specified hyper-parameter raster. The method thus allows
        to distribute a RasterStudy fit among multiple processes for multiprocessing.

        Parameters:
            idx - Index from 0 to (nJobs-1), indicating which part of the raster values are to be analyzed.

            nJobs - Number of processes to employ. Multiprocessing is based on the 'pathos' module.

            forwardOnly - If set to True, the fitting process is terminated after the forward pass. The resulting
                posterior distributions are so-called "filtering distributions" which - at each time step -
                only incorporate the information of past data points. This option thus emulates an online
                analysis.

            evidenceOnly - If set to True, only forward pass is run and evidence is calculated. In contrast to the
                forwardOnly option, no posterior mean values are computed and no posterior distributions are stored.

            customRaster - If set to True, the keyword argument 'raster' will not be used. Instead, all relevant
                attributes have to be set manually by the user. May be used for irregular grids of hyper-parameter
                values.

            silent - If set to True, no output is generated by the fitting method.

        Returns:
            RasterStudy instance
        """
        S = copy(self)
        S.rasterValues = np.array_split(S.rasterValues, nJobs)[idx]
        S.hyperParameterPrior = np.array_split(S.hyperParameterPrior, nJobs)[idx]

        for i, hyperParamValues in enumerate(S.rasterValues):
            S.setSelectedHyperParameters(hyperParamValues)

            # call fit method from parent class
            Study.fit(S, forwardOnly=forwardOnly, evidenceOnly=evidenceOnly, silent=True)

            S.logEvidenceList.append(S.logEvidence)
            S.localEvidenceList.append(S.localEvidence)
            if not evidenceOnly:
                S.averagePosteriorSequence += S.posteriorSequence *\
                                              np.exp(S.logEvidence - S.logEvidenceList[0]) *\
                                              S.hyperParameterPrior[i]

            if not silent:
                print '    + Process {} -- Analysis #{} of {}'.format(idx, i+1, len(S.rasterValues))

        print '    + Process {} finished.'.format(idx)
        return S

    # optimization methods are inherited from Study class, but cannot be used in this case
    def optimize(self, *args, **kwargs):
        print "! 'RasterStudy' object has no attribute 'optimize'"
        return

    def optimizationStep(self, *args, **kwargs):
        print "! 'RasterStudy' object has no attribute 'optimizationStep'"
        return

    def plotHyperParameterDistribution(self, param=0, **kwargs):
        """
        Creates a bar chart of a hyper-parameter distribution done with the RasterStudy class. The distribution is
        marginalized with respect to the hyper-parameter passed by name or index.

        Parameters:
            param - Parameter name or index of hyper-parameter to display; default: 0 (first model hyper-parameter)
            **kwargs - All further keyword-arguments are passed to the bar-plot (see matplotlib documentation)

        Returns:
            Two numpy arrays. The first array contains the hyper-parameter values, the second one the
            corresponding probability (density) values
        """
        hyperParameterNames = [name for name, lower, upper, steps in self.raster]

        if isinstance(param, (int, long)):
            paramIndex = param
        elif isinstance(param, basestring):
            paramIndex = -1
            for i, name in enumerate(hyperParameterNames):
                if name == param:
                    paramIndex = i

            # check if match was found
            if paramIndex == -1:
                print '! Wrong hyper-parameter name. Available options: {0}'.format(hyperParameterNames)
                return
        else:
            print '! Wrong parameter format. Specify parameter via name or index.'
            return

        axesToMarginalize = range(len(hyperParameterNames))
        axesToMarginalize.remove(paramIndex)

        # reshape hyper-parameter distribution for easy marginalizing
        rasterSteps = [steps for name, lower, upper, steps in self.raster]
        distribution = self.hyperParameterDistribution.reshape(rasterSteps, order='C')
        marginalDistribution = np.squeeze(np.apply_over_axes(np.sum, distribution, axesToMarginalize))

        # marginal distribution is not created by sum, but by the integral
        integrationFactor = np.prod([self.rasterConstant[axis] for axis in axesToMarginalize])
        marginalDistribution *= integrationFactor

        x = np.linspace(*self.raster[paramIndex][1:])
        plt.bar(x, marginalDistribution, align='center', width=self.rasterConstant[paramIndex], **kwargs)

        plt.xlabel(hyperParameterNames[paramIndex])

        # in case an integer step size for hyper-parameter values is chosen, probability is displayed
        # (probability density otherwise)
        if self.rasterConstant[paramIndex] == 1.:
            plt.ylabel('probability')
        else:
            plt.ylabel('probability density')

        return x, marginalDistribution

    def plotJointHyperParameterDistribution(self, params=[0, 1], figure=None, subplot=111,
                                            **kwargs):
        """
        Creates a 3D bar chart of a joint hyper-parameter distribution (of two hyper-parameters) done with the
        RasterStudy class. The distribution is marginalized with respect to the hyper-parameters passed by names or
        indices. Note that the 3D plot can only be included in an existing plot by passing a figure object and subplot
        specification.

        Parameters:
            params - List of two parameter names or indices of hyper-parameters to display; default: [0, 1]
                (first and second model parameter)

            figure - In case the plot is supposed to be part of an existing figure, it can be passed to the method. By
                default, a new figure is created.

            subplot - Characterization of subplot alignment, as in matplotlib. Default: 111

            **kwargs - all further keyword-arguments are passed to the bar3d-plot (see matplotlib documentation)

        Returns:
            Three numpy arrays. The first and second array contains the hyper-parameter values, the
            third one the corresponding probability (density) values
        """
        hyperParameterNames = [name for name, lower, upper, steps in self.raster]

        # check if list with two elements is provided
        if not isinstance(params, (list, tuple)):
            print '! A list of exactly two hyper-parameters has to be provided.'
            return
        elif not len(params) == 2:
            print '! A list of exactly two hyper-parameters has to be provided.'
            return

        # check for type of parameters (indices or names)
        if all(isinstance(p, (int, long)) for p in params):
            paramIndices = params
        elif all(isinstance(p, basestring) for p in params):
            paramIndices = []
            for i, name in enumerate(hyperParameterNames):
                for p in params:
                    if name == p:
                        paramIndices.append(i)

            # check if match was found
            if paramIndices == []:
                print '! Wrong hyper-parameter name. Available options: {0}'.format(hyperParameterNames)
                return
        else:
            print '! Wrong parameter format. Specify parameters either via name or index.'
            return

        # check if one of the parameter names provided is wrong
        if not len(paramIndices) == 2:
            print '! Probably one wrong hyper-parameter name. Available options: {0}'.format(hyperParameterNames)

        # check if parameter indices are in ascending order (so axes are labeled correctly)
        if not paramIndices[0] < paramIndices[1]:
            print '! Switching hyper-parameter order for plotting.'
            paramIndices = paramIndices[::-1]

        axesToMarginalize = range(len(hyperParameterNames))
        for p in paramIndices:
            axesToMarginalize.remove(p)

        # reshape hyper-parameter distribution for easy marginalizing
        rasterSteps = [steps for name, lower, upper, steps in self.raster]
        distribution = self.hyperParameterDistribution.reshape(rasterSteps, order='C')
        marginalDistribution = np.squeeze(np.apply_over_axes(np.sum, distribution, axesToMarginalize))

        # marginal distribution is not created by sum, but by the integral
        integrationFactor = np.prod([self.rasterConstant[axis] for axis in axesToMarginalize])
        marginalDistribution *= integrationFactor

        x, y = np.meshgrid(np.linspace(*self.raster[paramIndices[0]][1:]),
                           np.linspace(*self.raster[paramIndices[1]][1:]), indexing='ij')
        z = marginalDistribution
        print np.amax(z)

        # allow to add plot to predefined figure
        if figure is None:
            fig = plt.figure()
        else:
            fig = figure
        ax = fig.add_subplot(subplot, projection='3d')

        ax.bar3d(x.flatten() - self.rasterConstant[paramIndices[0]]/2.,
                 y.flatten() - self.rasterConstant[paramIndices[1]]/2.,
                 z.flatten()*0.,
                 self.rasterConstant[paramIndices[0]],
                 self.rasterConstant[paramIndices[1]],
                 z.flatten(),
                 zsort='max',
                 **kwargs
                 )

        ax.set_xlabel(hyperParameterNames[paramIndices[0]])
        ax.set_ylabel(hyperParameterNames[paramIndices[1]])

        # in case an integer step size for hyper-parameter values is chosen, probability is displayed
        # (probability density otherwise)
        if self.rasterConstant[paramIndices[0]]*self.rasterConstant[paramIndices[1]] == 1.:
            ax.set_zlabel('probability')
        else:
            ax.set_zlabel('probability density')

        return x, y, marginalDistribution
