#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Classes used for plotting ensemble coordination, e.g. phase correction, simulations"""


import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
import pandas as pd
import scipy.stats as stats
import seaborn as sns

from src import utils
import src.visualise.visualise_utils as vutils

FOLDER_PATH = 'coordination_plots'

__all__ = [
    'TriangleAxis', 'TrianglePlotChronology', 'RegPlotCouplingHalves', 'BarPlotSimulationComparison',
    'RegPlotCouplingGrangerCross', 'BarPlotCouplingCoefficients', 'HistPlotCouplingTerms', 'TrianglePlotTrack',
    'BarPlotModelComparison'
]


class BarPlotModelComparison(vutils.BasePlot):
    BAR_KWS = dict(
        errorbar=('ci', 95), estimator=np.nanmean, errwidth=2,
        errcolor=vutils.BLACK, edgecolor=vutils.BLACK, lw=2,
        n_boot=10, seed=1, capsize=0.1, width=0.8,
        palette=vutils.RGB, hue_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()
    )

    def __init__(self, model_df, **kwargs):
        self.corpus_title = kwargs.get('corpus_title', 'corpus_chronology')
        super().__init__(figure_title=fr'coordination_plots\barplot_modelcomparison_{self.corpus_title}', **kwargs)
        self.df = (
            model_df.set_index('instr')
            .loc[utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()]
            .reset_index(drop=False)
        )
        self.fig, self.ax = plt.subplots(
            nrows=2, ncols=1, sharex=True, sharey=False, figsize=(vutils.WIDTH / 2, vutils.WIDTH / 2)
        )
        self.hand, self.lab = None, None

    def _create_plot(self):
        for ax, var in zip(self.ax.flatten(), ['r2', 'bic']):
            sns.barplot(data=self.df, x='order', y=var, hue='instr', ax=ax, **self.BAR_KWS)

    def _format_ax(self):
        repeater = lambda x: [val for val in x for _ in range(0, 4)]
        for ax in self.ax.flatten():
            self.hand, self.lab = ax.get_legend_handles_labels()
            ax.get_legend().remove()
            plt.setp(ax.spines.values(), linewidth=2)
            ax.tick_params(axis='both', width=3)
            for patch, hatch in zip(ax.patches, repeater(vutils.HATCHES[:3])):
                patch.set_hatch(hatch)
        self.ax[0].set(ylabel=r'Mean $R^{2}_{adj}$', xlabel='', ylim=(0, 1))
        self.ax[1].set(ylabel='Mean BIC', ylim=(-1250, 0), xlabel='Model order ($k$)')

    def _format_fig(self):
        self.ax[1].legend(
            self.hand, ['Piano', 'Bass', 'Drums'],
            loc='lower right', title='Instrument', frameon=True,
            framealpha=1, edgecolor=vutils.BLACK
        )
        self.fig.tight_layout()


class TriangleAxis:
    """This class plots a single axes, showing the measured couplings between each musician in a trio.

    The direction of arrows indicates the influence and influencer instruments, namely the tendency of one performer
    to follow (and adapt to) the indicated instrument: the thickness and colour of the arrows indicate the strength
    of this coupling. This graph is similar to Figure 3b. in Jacoby, Polak, & London (2021).

    Args:
        grp (pd.DataFrame): the dataframe to plot, should contain information from one ensemble only.
        ax (plt.Axes): the axes object to plot onto.

    References:
        Jacoby, N., Polak, R., & London, J. (2021). Extreme precision in rhythmic interaction is enabled by
        role-optimized sensorimotor coupling: Analysis and modelling of West African drum ensemble music. Philosophical
        Transactions of the Royal Society B: Biological Sciences, 376(1835), 20200331.


    """
    img_loc = fr'{utils.get_project_root()}/references/images/musicians'

    def __init__(self, grp: pd.DataFrame, ax: plt.Axes, **kwargs):
        self.starting_zoom = kwargs.get('starting_zoom', 1)
        self.add_text = kwargs.get('add_text', True)
        self.arrow_mod = kwargs.get('arrow_mod', 25)
        self.len_mod = kwargs.get('len_mod', 1)
        self.text_mod = kwargs.get('text_mod', 0.1)
        self.head_width = kwargs.get('head_width', 20)
        self.piano_only = kwargs.get('piano_only', True)
        self.performer_picture_zoom = kwargs.get('performer_picture_zoom', 1)
        self.text_override = kwargs.get('text_override', None)
        self.ci = kwargs.get('ci', True)
        self.grp = grp
        self.ax = ax
        self.ax.axis('off')
        self.ax.set_aspect('equal')

    def create_plot(
            self
    ) -> plt.Axes:
        """Called from outside the class to generate the required plot elements, show them, and save"""
        self._add_musicians_images()
        if self.add_text:
            self._add_center_text()
        self._create_plot()
        return self.ax

    def _add_center_text(self):
        """Adds in text to the center of the plot"""
        # Add in the name of the musician
        txt = self.text_override
        if txt is None:
            txt = str(self.grp['pianist'].iloc[0]).replace(' ', '\n')
        self.ax.text(0.5, 0.5, txt, ha='center', va='center', fontsize=vutils.FONTSIZE * 2)

    def _create_plot(self,) -> None:
        """Creates the plot arrows and annotations, according to the modelled coupling responses."""
        # The starting coordinate for each arrow, i.e. the arrow tail
        start_coords = [
            [(0.15, 0.325), (0.85, 0.325)],
            [(0.35, 0.95), (0.725, 0.175)],
            [(0.65, 0.95), (0.275, 0.075), ],
        ]
        # The end coordinate for each arrow, i.e. the arrow head
        end_coords = [
            [(0.35, 0.75), (0.65, 0.75)],
            [(0.05, 0.325), (0.275, 0.175)],
            [(0.95, 0.325), (0.725, 0.075), ],
        ]
        # These values are the amount of rotation we require for each arrow
        rotation = [(62.5, -62.5), (62.5, 0), (-62.5, 0)]
        # Iterate over each influence instrument, their respective arrows, and the color they're associated with
        for influencer, start_coord, end_coord, col, rot12 in zip(
                utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(), start_coords, end_coords, vutils.RGB, rotation
        ):
            # Iterate over each instrument they influence, and each individual arrow
            for num, (influenced, (x, y), (x2, y2)) in enumerate(zip(
                    [i for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys() if i != influencer], start_coord, end_coord
            )):
                if self.piano_only and influencer != 'piano' and influenced != 'piano':
                    continue
                # Get our coupling coefficient
                c, q1, q2 = self._get_coupling_coefficient(influenced, influencer)
                # Add in the arrow
                if c > 0:
                    self.ax.annotate(
                        '', xy=(x, y), xycoords=self.ax.transAxes,
                        xytext=(x2, y2), textcoords=self.ax.transAxes,
                        arrowprops=dict(
                            width=c * self.arrow_mod, edgecolor=col, lw=1.5 * self.len_mod,
                            facecolor=col, headwidth=self.head_width
                        )
                    )
                if self.add_text:
                    # Add the coupling coefficient text in
                    txt = f'{round(abs(c), 2)}'
                    if self.ci:
                        txt += f"[{round(abs(q1), 2)}–{round(abs(q2), 2)}]"
                    self._add_coupling_coefficient_text(txt, x, x2, y, y2, rotation=rot12[num])

    def _get_coupling_coefficient(
            self, influenced: str, influencer: str
    ) -> tuple[float, float, float]:
        """Helper function to get the coupling coefficient between two instruments, the influencer and influenced."""
        # Get the coupling coefficients
        g = self.grp[self.grp['instrument'] == influenced][f'coupling_{influencer}'].dropna()
        # Bootstrap over all coupling coefficients to get confidence intervals
        boot_mean = [g.sample(frac=1, replace=True).mean() for _ in range(vutils.N_BOOT)]
        # Return the true mean and bootstrapped 95% confidence intervals
        return g.mean(), np.quantile(boot_mean, 0.05), np.quantile(boot_mean, 0.95)

    def _add_coupling_coefficient_text(
            self, constant, x, x2, y, y2, mod: float = 0.03, rotation: float = 0
    ) -> None:
        """Adds coupling coefficient text into the plot"""
        # Get the default annotation position, the midpoint of our arrow
        x_pos = (x + x2) / 2
        y_pos = (y + y2) / 2
        # Bottom of plot
        if y_pos < 0.3:
            y_pos += (mod * 1.5)
        # Top left of plot
        elif x_pos < 0.5:
            y_pos += mod
            x_pos -= (mod * 1.1)
        # Right of plot
        elif x_pos > 0.5:
            y_pos += mod
            x_pos += (mod * 1.1)
        # Add in the text using the x and y position
        self.ax.text(
            x_pos, y_pos, constant, ha='center', va='center', fontsize=vutils.FONTSIZE,
            rotation=rotation, rotation_mode='anchor'
        )

    def _add_musicians_images(
            self
    ) -> None:
        """Adds images corresponding to each performer in the trio"""
        # Set initial attributes for image plotting
        zoom, boxstyle = self.starting_zoom, 'square'
        img = None
        # Iterate through the position for each picture, the amount of zoom,
        # the name of the performer, and the colour of the box around the picture
        for (x, y), zoom, txt, col in zip(
                [(0.5, 0.875), (0.125, 0.125), (0.867, 0.133), ],
                [.68, .6375, .75, ],
                utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
                vutils.RGB
        ):
            # Get the number of observations for this instrument
            nobs = self.grp[self.grp['instrument'] == txt].shape[0]
            # Get the filepath from the performer's name
            mus = str(self.grp[self.grp['instrument'] == txt]['performer'].iloc[0]).replace(' ', '_').lower()
            # Try and get the image corresponding to the performers name
            try:
                img = plt.imread(fr'{self.img_loc}/{mus}.png')
            # If we don't have the image, use the default image corresponding to that instrument
            except FileNotFoundError:
                img = plt.imread(fr'{self.img_loc}/_{txt}.png')
            else:
                boxstyle = 'sawtooth'
                zoom *= self.performer_picture_zoom
            # Add the image into the graph, with the correct parameters and properties
            finally:
                img = mpl.offsetbox.OffsetImage(img, zoom=zoom * self.starting_zoom)
                ab = mpl.offsetbox.AnnotationBbox(
                    img, (x, y), xycoords='data', bboxprops=dict(edgecolor=col, lw=2, boxstyle=boxstyle)
                )
                self.ax.add_artist(ab)
            # Add the text in, adjacent to the image
            if self.add_text:
                self.ax.text(
                    x, y + self.text_mod if y < 0.5 else y - self.text_mod, txt.title(),
                    ha='center', va='center', color=col, fontsize=vutils.FONTSIZE,
                )
                # Add in the number of observations
                self.ax.text(
                    x, y - self.text_mod if y < 0.5 else y + self.text_mod, f'$n=$ {nobs}',
                    ha='center', va='center', color=col, fontsize=vutils.FONTSIZE,
                )


class TrianglePlotChronology(vutils.BasePlot):
    """Creates a triangle plot for each trio combination in the chronology corpus"""

    def __init__(self, df: pd.DataFrame, **kwargs):
        self.corpus_title = kwargs.get('corpus_title', 'corpus')
        super().__init__(figure_title=fr'{FOLDER_PATH}/triangleplot_trios_{self.corpus_title}', **kwargs)
        self.nobs_cutoff = kwargs.get('nobs_cutoff', 30)
        self.df = df[df['nobs'] < self.nobs_cutoff]
        self.fig, self.ax = plt.subplots(nrows=5, ncols=2, figsize=(vutils.WIDTH, vutils.WIDTH * 2))

    def _create_plot(self):
        """Create a `_TriangleAxis` object for each trio"""
        for a, (idx, grp) in zip(self.ax.flatten(), self.df.groupby('pianist')):
            self.g = TriangleAxis(grp, a).create_plot()

    def _format_fig(self):
        """Format figure-level parameters"""
        self.fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01, hspace=0.01, wspace=0.01)


class BarPlotCouplingCoefficients(vutils.BasePlot):
    """Creates bar plot showing coupling coefficients for each instrument"""
    nobs_cutoff = 30
    # TODO: fix this so that it'll work with the Bill Evans corpus, not just the chronology corpus

    def __init__(self, data, **kwargs):
        self.corpus_title = kwargs.get('corpus_title', 'corpus')
        super().__init__(figure_title=fr'{FOLDER_PATH}/barplot_couplingcoefficients_{self.corpus_title}',
                         **kwargs)
        self.df = (
            data.melt(
                id_vars=['instrument', 'performer', 'nobs', 'pianist'],
                value_vars=['coupling_bass', 'coupling_piano', 'coupling_drums'])
            .reset_index(drop=True)
        )
        self.fig, self.ax = plt.subplots(nrows=2, ncols=5, sharex=True, sharey=True, figsize=(vutils.WIDTH, 8))
        self.hand, self.lab = None, None
        if self.df['pianist'].nunique() < len(self.ax.flatten()):
            for band_num in range(len(self.ax.flatten()) - self.df['pianist'].nunique()):
                self.ax.flatten()[len(self.ax.flatten()) - band_num].axis('off')

    def _create_plot(self) -> None:
        """Creates main plotting object"""
        for ax, (idx, grp) in zip(self.ax.flatten(), self.df.groupby('pianist')):
            grp = grp[grp['nobs'] > self.nobs_cutoff]
            g = sns.barplot(
                data=grp, x='variable', y='value', hue='instrument', ax=ax,
                errorbar=('ci', 95), estimator=np.nanmean, errwidth=2,
                errcolor=vutils.BLACK, edgecolor=vutils.BLACK, lw=2,
                n_boot=10, seed=1, capsize=0.1, width=0.8
            )
            self.hand, self.lab = g.get_legend_handles_labels()
            g.get_legend().remove()
            g.set(title=idx)

    def _format_ax(self) -> None:
        """Formats axis-level parameters"""
        for ax in self.ax.flatten():
            ax.set(ylabel='', xlabel='', xticklabels=['Bass', 'Piano', 'Drums'], ylim=(0, 1))
            plt.setp(ax.spines.values(), linewidth=2)
            ax.tick_params(axis='both', width=3)

    def _format_fig(self) -> None:
        """Formats figure-level parameters"""
        self.fig.legend(
            self.hand, [i.title() for i in self.lab], title='Influenced\ninstrument', frameon=False,
            bbox_to_anchor=(1, 0.625), ncol=1, markerscale=1.6, fontsize=vutils.FONTSIZE
        )
        self.fig.supylabel('Coupling constant')
        self.fig.supxlabel('Influencer instrument')
        self.fig.subplots_adjust(top=0.95, bottom=0.1, left=0.075, right=0.885)


class RegPlotCouplingHalves(vutils.BasePlot):
    """Create a regression plot showing relationship between coupling in two halves of a piece"""
    # These are keywords that we pass into our given plot types
    REG_KWS = dict(
        scatter=False, ci=95, n_boot=vutils.N_BOOT
    )
    LINE_KWS = dict(
        lw=vutils.LINEWIDTH * 2, ls=vutils.LINESTYLE, zorder=5,
        color=vutils.BLACK
    )
    SCATTER_KWS = dict(
        hue_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
        palette=vutils.RGB, markers=['o', 's', 'D'], s=40,
        edgecolor=vutils.BLACK, zorder=3, alpha=vutils.ALPHA * 2,
        style_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
    )
    HIST_KWS = dict(
        kde=False, color=vutils.BLACK, alpha=vutils.ALPHA,
        lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE
    )
    TEXT_BBOX = dict(
        facecolor=vutils.WHITE, edgecolor=vutils.BLACK,
        lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE,
        boxstyle='round,pad=1'
    )

    def __init__(self, halves_df, **kwargs):
        self.corpus_title = 'corpus_chronology'
        # Initialise the base plot with our given kwargs
        super().__init__(figure_title=fr'{FOLDER_PATH}/regplot_recordinghalves_{self.corpus_title}', **kwargs)
        self.df = halves_df
        self.fig, self.ax = plt.subplots(
            nrows=2, ncols=2, figsize=(vutils.WIDTH / 2, vutils.WIDTH / 2),
            gridspec_kw=dict(width_ratios=(11, 1), height_ratios=(1, 5)),
        )
        # The main ax for plotting the regression/scatter plot
        self.main_ax = self.ax[1, 0]
        # Marginal ax, for plotting histograms
        self.marginal_ax = np.array([self.ax[0, 0], self.ax[1, 1]])
        # Top right corner ax, which we can go ahead and disable
        self.ax[0, 1].axis('off')

    def _create_main_plot(self) -> None:
        """Creates the main plot: scatter and regression plots"""
        sns.scatterplot(
            data=self.df, x='half_1', y='half_2', hue='variable', style='variable', ax=self.main_ax, **self.SCATTER_KWS
        )
        sns.regplot(data=self.df, x='half_1', y='half_2',  ax=self.main_ax, line_kws=self.LINE_KWS, **self.REG_KWS)

    def _create_marginal_plot(self) -> None:
        """Creates the marginal plot: density plots"""
        # Top marginal plot
        sns.histplot(
            data=self.df, x='half_1', ax=self.marginal_ax[0],
            bins=vutils.N_BINS,  **self.HIST_KWS
        )
        # Right marginal plot
        sns.histplot(
            data=self.df, y='half_2', ax=self.marginal_ax[1],
            bins=vutils.N_BINS,  **self.HIST_KWS
        )

    def _create_plot(self) -> None:
        """Creates both main and marginal plots"""
        self._create_main_plot()
        self._create_marginal_plot()

    def _format_main_ax(self) -> None:
        """Formats axis-level paramters on main plot"""
        # Add a grid onto the plot
        self.main_ax.grid(visible=True, axis='both', which='major', zorder=0, **vutils.GRID_KWS)
        # Get our legend handles, and set their edge color to black
        hand, _ = self.main_ax.get_legend_handles_labels()
        for ha in hand:
            ha.set_edgecolor(vutils.BLACK)
        # Remove the old legend, then add the new one on
        self.main_ax.get_legend().remove()
        self.main_ax.legend(
            hand, [i.title() for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()],
            loc='lower right', title='Influencer', frameon=True, framealpha=1,
            edgecolor=vutils.BLACK
        )
        # Final attributes to set here
        self.main_ax.set(
            xticks=[-0.5, 0, 0.5, 1.0, 1.5], xlim=(-0.5, 1.6), ylim=(-0.5, 1.6),
            xlabel='Coupling, first half', ylabel='Coupling, second half',
            yticks=[-0.5, 0, 0.5, 1.0, 1.5]
        )
        plt.setp(self.main_ax.spines.values(), linewidth=vutils.LINEWIDTH)
        self.main_ax.plot(
            self.main_ax.get_xlim(), self.main_ax.get_ylim(), lw=vutils.LINEWIDTH,
            ls='dashed', color=vutils.BLACK, alpha=vutils.ALPHA
        )
        self.main_ax.tick_params(axis='both', bottom=True, right=True, width=vutils.TICKWIDTH)

    def _format_marginal_ax(self) -> None:
        """Formats axis-level paramters on marginal plot"""
        # Remove correct spines from marginal axis
        for spine, ax in zip(['left', "bottom"], self.marginal_ax.flatten()):
            ax.spines[[spine, 'right', 'top']].set_visible(False)
            ax.tick_params(axis='both', width=vutils.TICKWIDTH)
            plt.setp(ax.spines.values(), linewidth=vutils.LINEWIDTH)
        # Set other features for the main axis
        self.marginal_ax[0].set(
            xlabel='', ylabel='', yticks=[0], yticklabels=[''], xticklabels=[],
            xlim=self.main_ax.get_xlim(), xticks=self.main_ax.get_xticks()
        )
        self.marginal_ax[1].set(
            xlabel='', ylabel='', xticks=[0], xticklabels=[''], yticklabels=[],
            ylim=self.main_ax.get_ylim(), yticks=self.main_ax.get_yticks()
        )

    def _add_regression_text(self) -> None:
        """Adds regression text to main axis"""
        r = self.df[['half_1', 'half_2']].corr().iloc[1].iloc[0]
        self.main_ax.text(
            -0.35, 1.4, rf'$r=${round(r, 2)}', bbox=self.TEXT_BBOX
        )

    def _format_ax(self) -> None:
        """Formats axis-level paramters for both main and marginal plot"""
        self._format_marginal_ax()
        self._format_main_ax()
        self._add_regression_text()

    def _format_fig(self) -> None:
        """Formats figure-level parameters"""
        self.fig.tight_layout()


class RegPlotCouplingGrangerCross(vutils.BasePlot):
    """Create a regression plot showing associations between phase correction, Granger causality and cross-corr"""
    COUPLING_COLS = ['coupling_piano', 'coupling_bass', 'coupling_drums']
    GRANGER_COLS = ['granger_causality_piano_i', 'granger_causality_bass_i', 'granger_causality_drums_i']
    CROSS_COLS = ['cross_corr_piano_r', 'cross_corr_bass_r', 'cross_corr_drums_r']

    # These are keywords that we pass into our given plot types
    REG_KWS = dict(
        scatter=False, ci=95, n_boot=vutils.N_BOOT
    )
    LINE_KWS = dict(
        lw=vutils.LINEWIDTH * 2, ls=vutils.LINESTYLE, zorder=5,
        color=vutils.BLACK
    )
    SCATTER_KWS = dict(
        hue_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
        palette=vutils.RGB, markers=['o', 's', 'D'], s=40,
        edgecolor=vutils.BLACK, zorder=3, alpha=vutils.ALPHA,
        style_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
    )
    HIST_KWS = dict(
        kde=False, color=vutils.BLACK, alpha=vutils.ALPHA,
        lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE
    )
    TEXT_BBOX = dict(
        facecolor=vutils.WHITE, edgecolor=vutils.BLACK,
        lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE,
        boxstyle='round,pad=1'
    )

    def __init__(self, model_df, **kwargs):
        self.corpus_title = 'corpus_chronology'
        # Initialise the base plot with our given kwargs
        super().__init__(figure_title=fr'{FOLDER_PATH}/regplot_couplinggrangercross_{self.corpus_title}', **kwargs)
        self.df = self._format_df(model_df)
        self.fig, self.ax = plt.subplots(
            nrows=2, ncols=3, figsize=(vutils.WIDTH, vutils.WIDTH / 2), sharey='row', sharex='col',
            gridspec_kw=dict(width_ratios=(1, 1, 0.2), height_ratios=(0.2, 1)),
        )
        # The main ax for plotting the regression/scatter plot
        self.main_ax = np.array(self.ax[[1, 0]])
        # Marginal ax, for plotting histograms
        self.marginal_ax = np.array([self.ax[0, 0], self.ax[0, 1], self.ax[-1, -1]])
        # Top right corner ax, which we can go ahead and disable
        self.ax[0, 2].axis('off')
        self.hand = None

    def _format_df(self, model_df: pd.DataFrame) -> pd.DataFrame:
        """Coerces provided `model_df` dataframe into correct format"""
        pc = model_df[self.COUPLING_COLS].melt(var_name='instrument', value_name='coupling').dropna()
        gc = model_df[self.GRANGER_COLS].melt(var_name='instrument', value_name='causality').dropna()['causality']
        cc = model_df[self.CROSS_COLS].melt(var_name='instrument', value_name='corr').dropna()['corr']
        conc = pd.concat([pc, gc, cc], axis=1)
        conc['instrument'] = conc['instrument'].str.replace('coupling_', '')
        return conc

    def _create_main_plot(self) -> None:
        """Creates main plotting object: regression and scatter plot between Granger, phase correction, cross-corr"""
        for a, var, xp in zip(self.main_ax.flatten(), ['causality', 'corr'], [-0.3, -0.0]):
            sns.scatterplot(
                data=self.df, x=var, y='coupling', hue='instrument', style='instrument', ax=a, **self.SCATTER_KWS
            )
            sns.regplot(data=self.df, x=var, y='coupling', ax=a, line_kws=self.LINE_KWS, **self.REG_KWS)
            self._add_regression_coeff(var, a, xp)

    def _add_regression_coeff(self, var: str, ax: plt.Axes, xpos: float) -> None:
        """Adds regression coefficient between coupling and given variable `var` to axis `ax` at position `xpos`"""
        r = self.df[['coupling', var]].corr().iloc[1].iloc[0]
        ax.text(xpos, 1.4, rf'$r=${round(r, 2)}', bbox=self.TEXT_BBOX)

    def _create_marginal_plot(self):
        """Creates marginal plot: density of all variables"""
        # Top marginal plots
        for num, var in enumerate(['causality', 'corr']):
            sns.histplot(
                data=self.df, x=var, ax=self.marginal_ax.flatten()[num],
                bins=vutils.N_BINS,  **self.HIST_KWS
            )
        # Right marginal plot
        sns.histplot(
            data=self.df, y='coupling', ax=self.marginal_ax.flatten()[-1],
            bins=vutils.N_BINS,  **self.HIST_KWS
        )

    def _create_plot(self) -> None:
        """Creates both main and marginal plotting objects"""
        self._create_main_plot()
        self._create_marginal_plot()

    def _format_main_ax(self) -> None:
        """Sets axis-level parameters for main plots (scatter/regression line)"""
        for ax, xl, xlab, ylab in zip(
                self.main_ax.flatten(), [(-0.4, 1.3), (-0.1, 0.9)],
                ['Granger index', 'Cross-correlation ($r$)'], ['Coupling', '']
        ):
            # Add a grid onto the plot
            ax.grid(visible=True, axis='both', which='major', zorder=0, **vutils.GRID_KWS)
            ax.tick_params(axis='both', bottom=True, right=True, width=vutils.TICKWIDTH)
            plt.setp(ax.spines.values(), linewidth=vutils.LINEWIDTH)
            ax.set(
                xlim=xl, ylim=(-0.5, 1.6),
                xlabel=xlab, ylabel=ylab,
                xticks=np.linspace(xl[0], xl[1], 5),
                yticks=[-0.5, 0, 0.5, 1.0, 1.5]
            )
            self.hand, _ = ax.get_legend_handles_labels()
            ax.get_legend().remove()
            ax.plot(
                ax.get_xlim(), ax.get_ylim(), lw=vutils.LINEWIDTH,
                ls='dashed', color=vutils.BLACK, alpha=vutils.ALPHA
            )

    def _format_marginal_ax(self) -> None:
        """Sets axis-level parameters for marginal plots (histogram/density)"""
        # Remove correct spines from marginal axis
        for spine, ax in zip(['left', 'left', "bottom"], self.marginal_ax.flatten()):
            ax.spines[[spine, 'right', 'top']].set_visible(False)
            ax.tick_params(axis='both', width=vutils.TICKWIDTH)
            plt.setp(ax.spines.values(), linewidth=vutils.LINEWIDTH)
            ax.set(xlabel='', ylabel='')
            if spine == 'left':
                ax.set(yticks=[0], yticklabels=[''])
            else:
                ax.set(xticks=[0], xticklabels=[''])

    def _format_ax(self) -> None:
        """Sets axis-level parameters for both main and marginal plots"""
        self._format_main_ax()
        self._format_marginal_ax()

    def _format_fig(self) -> None:
        """Sets figure-level parameters for plot"""
        self.fig.tight_layout()
        self.fig.legend(
            self.hand, [i.title() for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()],
            loc='upper right', title='Influencer', frameon=True, framealpha=1,
            edgecolor=vutils.BLACK
        )


class BarPlotCouplingCoefficients(vutils.BasePlot):
    """Creates bar plot of all coupling coefficients"""
    BAR_KWS = dict(
        dodge=True, errorbar=None, width=0.8, estimator=np.mean,
        zorder=3, hue_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
        ec=vutils.BLACK, ls=vutils.LINESTYLE, lw=vutils.LINEWIDTH, alpha=1,
    )

    def __init__(self, model_df, **kwargs):
        self.corpus_title = kwargs.get('corpus_title', 'corpus_chronology')
        super().__init__(figure_title=fr'coordination_plots/barplot_coefficients_{self.corpus_title}', **kwargs)
        self.df = (
            model_df.melt(
                id_vars=['mbz_id', 'instrument'],
                value_vars=['coupling_piano', 'coupling_bass', 'coupling_drums']
            )
            .dropna()
            .reset_index(drop=False)
            .set_index('instrument')
            .loc[utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()]
            .reset_index(drop=False)
        )
        instr = self.df['variable'].str.replace('coupling_', '').str.title()
        self.df['variable'] = self.df['instrument'].str.title() + '→' + instr
        self.fig, self.ax = plt.subplots(nrows=1, ncols=1, figsize=(vutils.WIDTH / 2, vutils.WIDTH / 4))

    def _create_plot(self) -> None:
        """Creates main plot: bar and scatter plot"""
        sns.barplot(data=self.df, x='variable', y='value', **self.BAR_KWS)
        sns.stripplot(data=self.df, x='variable', y='value', s=2, color=vutils.BLACK, zorder=5)
        marker_df = self.df.groupby('variable').mean().reset_index(drop=False)
        sns.stripplot(data=marker_df, x='variable', y='value', s=10, color='#8ffbfd', marker='s', zorder=10)

    def _format_ax(self) -> None:
        """Set axis-level parameters"""
        repeater = lambda x: [val for val in x for _ in (0, 1)]
        for patch, col, hatch in zip(self.ax.patches, repeater(vutils.RGB), repeater(vutils.HATCHES)):
            patch.set_facecolor(col)
            patch.set_hatch(hatch)
        self.ax.axhline(0, 0, 1, lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE, color=vutils.BLACK)
        self.ax.set_xticklabels(self.ax.get_xticklabels(), rotation=30, ha='right')
        self.ax.yaxis.grid(True, zorder=0, **vutils.GRID_KWS)
        plt.setp(self.ax.spines.values(), linewidth=vutils.LINEWIDTH)
        self.ax.tick_params(axis='both', bottom=True, right=True, width=vutils.TICKWIDTH)
        self.ax.set(xlabel='', ylabel='Coupling constant')

    def _format_fig(self) -> None:
        """Sets figure-level parameters"""
        self.fig.tight_layout()


class BarPlotSimulationComparison(vutils.BasePlot):
    """Creates barplot comparing between different simulation parameters"""
    BAR_KWS = dict(
        dodge=False, edgecolor=vutils.BLACK, errorbar=('ci', 95),
        lw=vutils.LINEWIDTH, seed=42, capsize=0.1, width=0.8,
        ls=vutils.LINESTYLE, estimator=np.mean,
        errcolor=vutils.BLACK, zorder=3, alpha=1
    )
    TRIANGLE_KWS = dict(
        starting_zoom=0.3, arrow_mod=7.5, add_text=False, head_width=7.5, len_mod=0.8
    )
    PAL = ['#6fcbdc', *vutils.RGB, '#6fcbdc', '#6fcbdc']
    HATCHES = ['', *vutils.HATCHES[:3], '', '']

    def __init__(self, all_sims, all_params: list, **kwargs):
        self.corpus_title = kwargs.get('corpus_title', 'corpus_chronology')
        super().__init__(figure_title=fr'coordination_plots/barplot_simulationcomparison_{self.corpus_title}', **kwargs)
        self.df = self._format_df(all_sims)
        self.params = all_params
        self.fig = plt.figure(figsize=(vutils.WIDTH, vutils.WIDTH / 3))
        gs = self.fig.add_gridspec(2, 6, height_ratios=[3, 1])
        self.main_ax = self.fig.add_subplot(gs[0, :])
        self.small_axs = np.array([self.fig.add_subplot(gs[1, i]) for i in range(len(all_sims))])

    @staticmethod
    def _format_df(all_sims: list) -> pd.DataFrame:
        """Coerces provided list of `Simulation` objects `all_sims` into a dataframe of RMS asynchrony values"""
        names = ['original', 'piano_leader', 'bass_leader', 'drums_leader', 'democracy', 'anarchy']
        all_rms = (
            pd.DataFrame([s.get_rms_values() for s in all_sims])
            .transpose()
            .rename(columns={i: k for i, k in zip(range(6), names)})
        )
        shift_value = all_rms['original'].mean()
        all_rms = all_rms.melt()
        all_rms['value'] /= shift_value
        all_rms.loc[all_rms['variable'] == 'anarchy', 'value'] = 7
        return all_rms

    @staticmethod
    def _format_params(param_dict: dict) -> pd.DataFrame:
        """Format the parameters for a given `TriangleAxis` plot"""
        small = (
            pd.DataFrame(param_dict)
            .transpose()
            .reset_index(drop=False)
            .rename(columns={'index': 'instrument'})
        )
        small['pianist'] = ''
        small['performer'] = ''
        return small

    def _create_plot(self) -> None:
        """Creates main plot: a combination of bar plot and custom `TriangleAxis`"""
        sns.barplot(data=self.df, x='variable', y='value', ax=self.main_ax, **self.BAR_KWS)
        for ax, param_dict in zip(self.small_axs, self.params):
            TriangleAxis(self._format_params(param_dict), ax, **self.TRIANGLE_KWS).create_plot()

    def _format_ax(self) -> None:
        """Sets axis-level parameters for all axis objects"""
        self.main_ax.set(
            ylim=(0, 7), xlabel='', yticks=[1, 2, 3, 4, 5, 6, 7], yticklabels=['1', '2', '3', '4', '5', '6', '>6'],
            xticklabels=[
                'Original', 'Leadership\npiano', 'Leadership\nbass',
                'Leadership\ndrums', 'Equally\nbalanced', 'No\ncoupling'
            ]
        )
        self.main_ax.set_ylabel('Normalized RMS of the simulated\nasynchrony (1 = original RMS)', x=-0.01)
        plt.setp(self.main_ax.spines.values(), linewidth=vutils.LINEWIDTH, color=vutils.BLACK)
        self.main_ax.tick_params(axis='both', width=vutils.TICKWIDTH, color=vutils.BLACK)
        # Add a vertical grid
        self.main_ax.grid(zorder=0, axis='y', **vutils.GRID_KWS)
        for patch, col, hatch in zip(self.main_ax.patches, self.PAL, self.HATCHES):
            patch.set_facecolor(col)
            patch.set_hatch(hatch)
        for line in self.main_ax.lines:
            line.set_zorder(5)

    def _format_fig(self) -> None:
        """Sets figure-level parameters"""
        self.fig.subplots_adjust(wspace=0.1, hspace=0.3, left=0.095, right=0.975, top=0.95, bottom=0.05)


class HistPlotCouplingTerms(vutils.BasePlot):
    """Creates histogram plot showing differences in coupling coefficients extracted from models"""
    HIST_KWS = dict(lw=vutils.LINEWIDTH / 2, ls=vutils.LINESTYLE, zorder=2, align='edge')
    KDE_KWS = dict(linestyle=vutils.LINESTYLE, alpha=1, zorder=3, linewidth=vutils.LINEWIDTH)
    TITLES = [r'Self coupling ($\alpha_{i,i}$)', r'Partner coupling ($\alpha_{i,j}$)',
              r'Intercept ($\alpha_{i,0}$)']

    def __init__(self, coupling_df, **kwargs):
        self.corpus_title = 'corpus_chronology'
        super().__init__(figure_title=fr'coordination_plots/histplot_couplingterms_{self.corpus_title}', **kwargs)
        self.df = (
            coupling_df.set_index('instrument')
            .loc[utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()]
            .reset_index(drop=False)
        )
        self.fig, self.ax = plt.subplots(
            nrows=3, ncols=3, sharex='col', sharey=True, figsize=(vutils.WIDTH, vutils.WIDTH)
        )

    @staticmethod
    def _kde(dat, len_data: int = 1000) -> tuple:
        """Creates KDE and fits to linear space of values, then scales between 0 and 1"""
        # Fit the actual KDE to the data, using the default parameters
        kde = stats.gaussian_kde(dat.T)
        # Create a linear space of integers ranging from our lowest to our highest BUR
        data_plot = np.linspace(dat.min(), dat.max(), len_data)[:, np.newaxis]
        # Evaluate the KDE on our linear space of integers
        y = kde.evaluate(data_plot.T)
        return data_plot, np.array([(y_ - min(y)) / (max(y) - min(y)) for y_ in y])

    @staticmethod
    def _hist(dat: np.array, n_bins: int = 30) -> tuple:
        """Creates histogram for given data, returns histogram edges, heights, and widths"""
        heights, edges = np.histogram(dat, bins=n_bins)
        heights = heights / max(heights)
        return edges[:-1], heights, np.diff(edges)

    def _create_plot(self) -> None:
        """Creates main plotting object"""
        num = 0
        ax_flat = self.ax.flatten()
        for (idx, grp), col in zip(self.df.clean.groupby('instrument', sort=False), vutils.RGB):
            self_coupling = grp['self_coupling'].values
            intercept = grp['intercept'].values
            partner_coupling = grp[
                [f'coupling_{i}' for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys() if i != idx]
            ].values.flatten()
            for variable in [self_coupling, partner_coupling, intercept]:
                x, height, width = self._hist(variable)
                ax_flat[num].bar(
                    x=x, height=height, width=width, fc=col, edgecolor='None', alpha=vutils.ALPHA, **self.HIST_KWS
                )
                ax_flat[num].bar(
                    x=x, height=height, width=width, fc='None', edgecolor=vutils.BLACK, alpha=1, **self.HIST_KWS
                )
                x, y = self._kde(variable)
                ax_flat[num].plot(x, y, color=col, **self.KDE_KWS)
                num += 1

    def _format_ax(self) -> None:
        """Format axis-level parameters"""
        ax_flat = self.ax.flatten()
        for num, inst in zip([0, 3, 6], utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()):
            ax_flat[num].set_ylabel(inst.title())
        for num, tit in zip([0, 1, 2], self.TITLES):
            ax_flat[num].set_title(tit)
        for num, tit in zip([6, 7, 8], self.TITLES):
            ax_flat[num].set_xlabel(tit)
        for ax in ax_flat:
            plt.setp(ax.spines.values(), linewidth=vutils.LINEWIDTH)
            ax.tick_params(axis='both', width=vutils.TICKWIDTH)
            ax.grid(axis='x', which='major', **vutils.GRID_KWS)
            ax.axvline(0, 0, 1, color=vutils.BLACK, linewidth=vutils.LINEWIDTH * 2, linestyle='dashed')

    def _format_fig(self) -> None:
        """Formats figure-level parameters"""
        self.fig.supylabel('Density')
        self.fig.supxlabel('Model coefficient')
        self.fig.tight_layout()


class TrianglePlotTrack(vutils.BasePlot):
    BAR_KWS = dict(
        dodge=True, errorbar=None, width=0.8, estimator=np.mean,
        zorder=3, hue_order=utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys(),
        ec=vutils.BLACK, ls=vutils.LINESTYLE, lw=vutils.LINEWIDTH, alpha=1,
    )

    def __init__(self, onset_maker):
        self.df = self._format_df(onset_maker)
        self.fname = rf'onsets_plots/triangleplot_coordination_{onset_maker.item["mbz_id"]}'
        self.title = onset_maker.item['fname']
        super().__init__(figure_title=self.fname)
        self.bar_df = self._format_bar_df()
        self.fig, self.ax = plt.subplots(nrows=1, ncols=2, figsize=(vutils.WIDTH, vutils.WIDTH / 3))

    @staticmethod
    def _format_df(om):
        from src.features.rhythm_features import PhaseCorrection
        sd = pd.DataFrame(om.summary_dict)
        res = []
        for my_instr in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys():
            their_instrs = [i for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys() if i != my_instr]
            my_beats = sd[my_instr]
            their_beats = sd[their_instrs]
            pc = PhaseCorrection(my_beats=my_beats, their_beats=their_beats)
            for _ in range(int(pc.summary_dict['nobs'])):
                res.append({'instrument': my_instr, 'pianist': om.item['pianist'],
                            'performer': om.item['musicians'][
                                utils.INSTRUMENTS_TO_PERFORMER_ROLES[my_instr]]} | pc.summary_dict)
        return pd.DataFrame(res)

    def _format_bar_df(self):
        model_df = (
            self.df.melt(
                id_vars=['instrument'],
                value_vars=['coupling_piano', 'coupling_bass', 'coupling_drums']
            )
            .dropna()
            .reset_index(drop=False)
        )
        instr = model_df['variable'].str.replace('coupling_', '').str.title()
        model_df['variable'] = instr + '→' + model_df['instrument'].str.title()
        model_df['instrument'] = instr
        return (
            model_df.set_index('instrument')
            .loc[[i.title() for i in utils.INSTRUMENTS_TO_PERFORMER_ROLES.keys()]]
            .reset_index(drop=False)
        )

    def _create_plot(self):
        ta = TriangleAxis(
            self.df, self.ax[0], piano_only=False, text_mod=0.15, text_override='', ci=False
        )
        ta.create_plot()
        sns.barplot(data=self.bar_df, ax=self.ax[1], x='variable', y='value', **self.BAR_KWS)

    def _format_ax(self) -> None:
        """Set axis-level parameters"""
        repeater = lambda x: [val for val in x for _ in (0, 1)]
        for patch, col, hatch in zip(self.ax[1].patches, repeater(vutils.RGB), repeater(vutils.HATCHES)):
            patch.set_facecolor(col)
            patch.set_hatch(hatch)
        self.ax[1].axhline(0, 0, 1, lw=vutils.LINEWIDTH, ls=vutils.LINESTYLE, color=vutils.BLACK)
        self.ax[1].set_xticklabels(self.ax[1].get_xticklabels(), rotation=30, ha='right')
        self.ax[1].yaxis.grid(True, zorder=0, **vutils.GRID_KWS)
        plt.setp(self.ax[1].spines.values(), linewidth=vutils.LINEWIDTH)
        self.ax[1].tick_params(axis='both', bottom=True, right=True, width=vutils.TICKWIDTH)
        self.ax[1].set(xlabel='Direction of influence', ylabel='Coupling constant')

    def _format_fig(self) -> None:
        """Sets figure-level parameters"""
        self.fig.suptitle(self.title)
        self.fig.subplots_adjust(top=0.9, bottom=0.25, left=0., right=0.95)
        self.ax[0].set_position([-0.15, 0.1, 0.75, 0.75])


if __name__ == '__main__':
    pass
